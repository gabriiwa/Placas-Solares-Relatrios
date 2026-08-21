#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Relatório solar diário — Nansen Solar / AUXSOL -> Google Chat.

Roda no GitHub Actions todo dia às 7h (America/Sao_Paulo). Fluxo:

  1. autentica na API AUXSOL (app_id / app_secret -> Bearer, 12 h)
  2. lista as usinas da conta (ou usa PLANT_IDS do .env)
  3. puxa geração do dia, do mês e alarmes ativos
  4. calcula eficiência de forma determinística (Python, sem IA)
  5. opcionalmente pede um comentário curto ao Gemini
  6. envia um Card v2 para o webhook de um espaço do Google Chat

Só biblioteca padrão: nada de pip install, nada de requirements.txt.

Subcomandos
-----------
  python main.py relatorio        # padrão: monta e envia o card
  python main.py descobrir-url    # testa candidatos de AUXSOL_BASE_URL
  python main.py listar-usinas    # imprime id / nome / kWp de cada usina
  python main.py alarmes          # imprime os alarmes crus
  python main.py testar-webhook   # manda um card de teste pro Google Chat

Variáveis de ambiente
---------------------
  AUXSOL_BASE_URL     obrigatória, ex: https://www.auxsolcloud.com/prod-api
  AUXSOL_APP_ID       obrigatória
  AUXSOL_APP_SECRET   obrigatória
  AUXSOL_LANG         opcional, padrão pt_BR (cai para en_US se a API recusar)
  PLANT_IDS           opcional, ex: "101,102,103". Vazio = todas as usinas.
  GCHAT_WEBHOOK_URL   obrigatória para enviar (não para DRY_RUN)
  GEMINI_API_KEY      opcional. Sem ela, o card sai sem o parágrafo da IA.
  GEMINI_MODEL        opcional, padrão gemini-flash-latest
  CAPACIDADE_KWP      opcional, ex: "Tijucas=60,Bairro Novo=60,Makiolka=60"
                      casamento por pedaço do nome, sem acento, sem caixa.
  KWH_POR_KWP_ESPERADO  opcional, padrão 4.2 (referência de dia bom no Sul/PR)
  DRY_RUN             "1" imprime o payload em vez de enviar
  DEBUG               "1" loga cada request/response (sem segredos)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover
    TZ = None

# --------------------------------------------------------------------------- #
# configuração
# --------------------------------------------------------------------------- #

BASE_URL = (os.environ.get("AUXSOL_BASE_URL") or "").rstrip("/")
APP_ID = os.environ.get("AUXSOL_APP_ID", "").strip()
APP_SECRET = os.environ.get("AUXSOL_APP_SECRET", "").strip()
LANG = os.environ.get("AUXSOL_LANG", "pt_BR").strip() or "pt_BR"
PLANT_IDS = [p.strip() for p in os.environ.get("PLANT_IDS", "").split(",") if p.strip()]
GCHAT_WEBHOOK_URL = os.environ.get("GCHAT_WEBHOOK_URL", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
# Atenção ao padrão: o workflow passa `vars.GEMINI_MODEL` sempre, então a
# variável EXISTE vazia quando a Variable não foi criada — e nesse caso o
# default do os.environ.get não vale. O `or` é que garante o padrão.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-flash-latest"
DRY_RUN = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")
DEBUG = os.environ.get("DEBUG", "").strip() in ("1", "true", "yes")
KWH_POR_KWP_ESPERADO = float(os.environ.get("KWH_POR_KWP_ESPERADO", "4.2") or 4.2)

TIMEOUT = 30
UA = "relatorio-solar-b4/1.0 (+github-actions)"

# Onde mora o front-end da plataforma. O `descobrir-url` baixa o bundle
# JavaScript destes endereços e lê de lá o prefixo real da API.
FRONTENDS = [
    "https://www.auxsolcloud.com",
    "https://www.nansensolar.com.br/monitoramento",
]

# CONFIRMADO em 21/08/2026, lendo as requisições reais do front-end logado:
#   https://eu.auxsolcloud.com/auxsol-api/system/dict/data/all
# O `www` serve só o front-end estático (por isso os 404 em /prod-api); a API
# mora num host REGIONAL, com prefixo /auxsol-api.
PREFIXO_CONFIRMADO = "/auxsol-api"

# A região vista foi a `eu`. Uma conta brasileira pode estar em outra, então
# todas são testadas — a que autenticar é a certa.
REGIOES = ["eu", "br", "sa", "us", "ap", "cn", "global", "www"]

# Prefixos alternativos, caso a plataforma mude de padrão no futuro.
PREFIXOS = [PREFIXO_CONFIRMADO, "/prod-api", "/api", "/stage-api", "/openapi", ""]

HOST_PRINCIPAL = "https://eu.auxsolcloud.com"

CANDIDATOS_BASE = (
    # primeiro o combo confirmado, região por região
    [f"https://{r}.auxsolcloud.com{PREFIXO_CONFIRMADO}" for r in REGIOES]
    # depois os outros prefixos no host confirmado
    + [HOST_PRINCIPAL + p for p in PREFIXOS[1:]]
)

# Bitmask de séries do queryPlantReportByPlantId.
# bits 6..13 = acumulados em kWh. 6 geração, 7 comprado da rede,
# 8 injetado, 11 consumo da carga, 12 autoconsumo da geração.
DATA_ITEMS_ENERGIA = (1 << 6) | (1 << 7) | (1 << 8) | (1 << 11) | (1 << 12)


def log(msg: str) -> None:
    print(msg, flush=True)


def dbg(msg: str) -> None:
    if DEBUG:
        print(f"[debug] {msg}", flush=True)


def agora() -> datetime:
    return datetime.now(TZ) if TZ else datetime.now()


def sem_acento(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn"
    ).lower()


# --------------------------------------------------------------------------- #
# HTTP com respeito ao limite de 10 requisições / 5 s por IP
# --------------------------------------------------------------------------- #

_janela: list[float] = []


def _throttle() -> None:
    global _janela
    agora_s = time.monotonic()
    _janela = [t for t in _janela if agora_s - t < 5.0]
    if len(_janela) >= 9:  # margem: 9 em vez de 10
        espera = 5.0 - (agora_s - _janela[0]) + 0.05
        if espera > 0:
            dbg(f"throttle: aguardando {espera:.2f}s")
            time.sleep(espera)
        _janela = [t for t in _janela if time.monotonic() - t < 5.0]
    _janela.append(time.monotonic())


def http(
    url: str,
    metodo: str = "GET",
    corpo: dict | None = None,
    params: dict | None = None,
    headers: dict | None = None,
    tentativas: int = 3,
    timeout: int = TIMEOUT,
) -> tuple[int, Any]:
    """Devolve (status, json_ou_texto). Não levanta em erro HTTP."""
    if params:
        limpos = {k: v for k, v in params.items() if v not in (None, "")}
        if limpos:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(limpos)}"

    dados = json.dumps(corpo).encode() if corpo is not None else None
    hdr = {"User-Agent": UA, "Accept": "application/json"}
    if dados is not None:
        hdr["Content-Type"] = "application/json"
    hdr.update(headers or {})

    ultimo_erro: Exception | None = None
    for tentativa in range(1, tentativas + 1):
        _throttle()
        req = urllib.request.Request(url, data=dados, headers=hdr, method=metodo)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                bruto = r.read().decode("utf-8", "replace")
                status = r.status
        except urllib.error.HTTPError as e:
            bruto = e.read().decode("utf-8", "replace")
            status = e.code
        except Exception as e:  # timeout, DNS, TLS...
            ultimo_erro = e
            dbg(f"{metodo} {url} falhou ({e}); tentativa {tentativa}/{tentativas}")
            if tentativa < tentativas:
                time.sleep(2 * tentativa)
                continue
            return 0, str(e)

        try:
            payload: Any = json.loads(bruto) if bruto.strip() else {}
        except json.JSONDecodeError:
            payload = bruto

        dbg(f"{metodo} {url} -> {status} {str(payload)[:300]}")

        # 429 / 5xx merecem nova tentativa
        if status in (429, 500, 502, 503, 504) and tentativa < tentativas:
            time.sleep(2 * tentativa)
            continue
        return status, payload

    return 0, str(ultimo_erro)


# --------------------------------------------------------------------------- #
# cliente AUXSOL
# --------------------------------------------------------------------------- #

SUCESSO = {"AWX-0000", "0", 0, 200}


def _ok(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    codigo = payload.get("code", payload.get("resultCode"))
    return codigo in SUCESSO or str(codigo) in {str(c) for c in SUCESSO}


def _dados(payload: Any) -> Any:
    if isinstance(payload, dict):
        for chave in ("data", "result", "rows", "records"):
            if chave in payload:
                return payload[chave]
    return payload


def _msg(payload: Any) -> str:
    if isinstance(payload, dict):
        for chave in ("msg", "message", "errorMsg", "resultMsg"):
            if payload.get(chave):
                return str(payload[chave])
    return str(payload)[:200]


class Auxsol:
    def __init__(self, base_url: str, app_id: str, app_secret: str, lang: str = LANG):
        self.base = base_url.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self.lang = lang
        self.token: str | None = None

    # -- autenticação ------------------------------------------------------ #

    def autenticar(self, rapido: bool = False) -> str:
        """rapido=True: um idioma, uma tentativa, timeout curto (sondagem de URL)."""
        ultimo = ""
        langs = [self.lang] if rapido else dict.fromkeys([self.lang, "en_US", "zh_CN"])
        for lang in langs:
            status, payload = http(
                f"{self.base}/auth/token",
                metodo="POST",
                corpo={"app_id": self.app_id, "app_secret": self.app_secret, "lang": lang},
                tentativas=1 if rapido else 3,
                timeout=8 if rapido else TIMEOUT,
            )
            if status == 200 and _ok(payload):
                d = _dados(payload) or {}
                token = None
                if isinstance(d, dict):
                    token = (
                        d.get("access_token")
                        or d.get("token")
                        or d.get("accessToken")
                        or d.get("bearer")
                    )
                elif isinstance(d, str):
                    token = d
                if token:
                    self.token = str(token)
                    self.lang = lang
                    dbg(f"autenticado (lang={lang})")
                    return self.token
            ultimo = f"HTTP {status}: {_msg(payload)}"
        raise RuntimeError(f"falha na autenticação — {ultimo}")

    def _hdr(self) -> dict:
        if not self.token:
            self.autenticar()
        return {"Authorization": f"Bearer {self.token}", "Accept-Language": self.lang}

    def get(self, caminho: str, params: dict | None = None, corpo: dict | None = None) -> Any:
        """
        Vários GETs da AUXSOL são documentados COM corpo JSON. Mandamos os
        mesmos campos na query string e no corpo, para tolerar as duas leituras.
        """
        url = f"{self.base}{caminho}"
        juntos = {**(params or {}), **(corpo or {})}
        status, payload = http(url, "GET", corpo=(juntos or None), params=juntos, headers=self._hdr())

        # token expirado -> renova uma vez
        if status in (401, 403) or (isinstance(payload, dict) and "token" in _msg(payload).lower()):
            dbg("token recusado; reautenticando")
            self.token = None
            status, payload = http(
                url, "GET", corpo=(juntos or None), params=juntos, headers=self._hdr()
            )

        if status != 200 or not _ok(payload):
            raise RuntimeError(f"{caminho} -> HTTP {status}: {_msg(payload)}")
        return _dados(payload)

    # -- endpoints --------------------------------------------------------- #

    def usinas(self) -> list[dict]:
        d = self.get("/archive/plant/list", {"pageNum": 1, "pageSize": 200})
        if isinstance(d, dict):
            d = d.get("list") or d.get("rows") or d.get("records") or []
        return [x for x in (d or []) if isinstance(x, dict)]

    def dados_atuais_todas(self) -> Any:
        return self.get("/analysis/plantReport/queryPlantCurrentDataAll")

    def dados_atuais(self, plant_id: str) -> Any:
        return self.get(f"/analysis/plantReport/queryPlantCurrentData/{plant_id}")

    def serie(self, plant_id: str, parametro: str, valor: Any, tempo: str) -> Any:
        """Série histórica, com o nome do parâmetro de granularidade variável."""
        return self.get(
            "/analysis/plantReport/queryPlantReportByPlantId",
            {
                "plantId": plant_id,
                parametro: valor,
                "queryTime": tempo,
                "dataItems": DATA_ITEMS_ENERGIA,
            },
        )

    def relatorio_usina(self, plant_id: str, tipo: int = 2, data: str | None = None) -> Any:
        """tipo: 1=dia 2=mês 3=ano 4=total (nomenclatura da AUXSOL)."""
        return self.get(
            "/analysis/plantReport/queryPlantReportByPlantId",
            {
                "plantId": plant_id,
                "timeType": tipo,
                "queryTime": data or agora().strftime("%Y-%m-%d"),
                "dataItems": DATA_ITEMS_ENERGIA,
            },
        )

    def alarmes(self) -> list[dict]:
        d = self.get("/analysis/alarm/list", {"pageNum": 1, "pageSize": 200})
        if isinstance(d, dict):
            d = d.get("list") or d.get("rows") or d.get("records") or []
        return [x for x in (d or []) if isinstance(x, dict)]


# --------------------------------------------------------------------------- #
# leitura tolerante de campos (o shape exato da resposta não é conhecido)
# --------------------------------------------------------------------------- #


def achatar(obj: Any, prefixo: str = "") -> dict:
    """Achata dicts/listas aninhados em {caminho: valor_escalar}."""
    saida: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            saida.update(achatar(v, f"{prefixo}.{k}" if prefixo else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            saida.update(achatar(v, f"{prefixo}[{i}]"))
    else:
        saida[prefixo] = obj
    return saida


def pegar(obj: Any, candidatos: Iterable[str], padrao: Any = None) -> Any:
    """Procura a primeira chave cujo nome final case com um dos candidatos."""
    plano = achatar(obj)
    normal = {k: sem_acento(k.split(".")[-1].split("[")[0]) for k in plano}
    for c in candidatos:
        alvo = sem_acento(c)
        for k, n in normal.items():
            if n == alvo and plano[k] not in (None, ""):
                return plano[k]
    for c in candidatos:  # segunda passada: casamento parcial
        alvo = sem_acento(c)
        for k, n in normal.items():
            if alvo in n and plano[k] not in (None, ""):
                return plano[k]
    return padrao


def num(v: Any, padrao: float | None = None) -> float | None:
    if v in (None, "", "-"):
        return padrao
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return padrao


def primeiro(*valores: Any) -> Any:
    """Primeiro valor que não é None. Cuidado: 0.0 é dado válido, não ausência."""
    for v in valores:
        if v is not None:
            return v
    return None


def capacidades_configuradas() -> dict[str, float]:
    saida: dict[str, float] = {}
    for par in os.environ.get("CAPACIDADE_KWP", "").split(","):
        if "=" in par:
            nome, valor = par.split("=", 1)
            kwp = num(valor)
            if kwp:
                saida[sem_acento(nome.strip())] = kwp
    return saida


# --------------------------------------------------------------------------- #
# análise determinística
# --------------------------------------------------------------------------- #

# Nomes CONFIRMADOS em 21/08/2026 contra a API real da Nansen (endpoint
# /archive/plant/list). Os alternativos ficam para trás como rede de segurança
# caso a plataforma mude — o `pegar()` tenta na ordem.
CAMPOS_NOME = ["plantName", "name", "stationName", "plant_name"]
CAMPOS_ID = ["plantId", "id", "stationId", "plant_id"]
CAMPOS_KWP = ["capacity", "installedCapacity", "kwp", "totalCapacity", "designPower"]
CAMPOS_DIA = ["todayYield", "dayYield", "dayEnergy", "todayEnergy", "eDay", "energyToday"]
CAMPOS_MES = ["monthlyYield", "monthYield", "monthEnergy", "eMonth", "monthlyEnergy"]
CAMPOS_TOTAL = ["totalYield", "totalEnergy", "eTotal", "cumulativeEnergy"]
CAMPOS_POT = ["currentPower", "activePower", "nowPower", "realPower"]
CAMPOS_STATUS = ["plantStatus", "status", "runStatus", "state", "deviceStatus"]
# fullLoadHour é exatamente o kWh/kWp do dia, já calculado pela plataforma.
CAMPOS_HORAS = ["fullLoadHour", "fullLoadHours", "equivalentHour", "equivalentHours"]
# Última comunicação do datalogger: delata equipamento mudo.
CAMPOS_DT = ["dt", "updateTime", "lastUpdateTime", "collectTime", "dataTime"]
# Tarifa cadastrada na própria plataforma (vem em tariff.fixPrice).
CAMPOS_TARIFA = ["fixPrice", "unitPrice", "electricityPrice"]

# status "01" = normal, confirmado nas 3 usinas em operação. Os outros códigos
# não foram observados, então não são adivinhados: aparecem crus no card.
STATUS_CONHECIDO = {"01": "normal"}

# A credencial é de convidado, então os nomes vêm com esse sufixo. Ninguém
# precisa ler "(Guest)" três vezes num card às 7h da manhã.
SUFIXOS_LIXO = ("(guest)", "(visitor)", "(visitante)", "(convidado)")


def limpar_nome(nome: str) -> str:
    limpo = str(nome or "").strip()
    baixo = limpo.lower()
    for sufixo in SUFIXOS_LIXO:
        if baixo.endswith(sufixo):
            limpo = limpo[: -len(sufixo)].strip()
            break
    return limpo or "usina sem nome"


# Forma da curva anual de irradiação para a região de Curitiba (kWh/m²/dia em
# plano inclinado). O que importa aqui é a PROPORÇÃO entre os meses, não o valor
# absoluto: agosto rende cerca de 60% de dezembro, e comparar os dois com um
# número único faz o inverno parecer defeito.
FORMA_MENSAL = {
    1: 5.00, 2: 5.00, 3: 4.60, 4: 4.20, 5: 3.60, 6: 3.30,
    7: 3.50, 8: 4.30, 9: 4.20, 10: 4.70, 11: 5.40, 12: 5.40,
}
DIAS_MES = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
# Σ(forma × dias): converte "kWh/kWp no ano" em "kWh/kWp por dia neste mês".
SOMA_PONDERADA = sum(FORMA_MENSAL[m] * DIAS_MES[m] for m in FORMA_MENSAL)
# Desempenho típico de sistema (perdas de inversor, cabo, temperatura, sujeira).
RENDIMENTO_SISTEMA = 0.80


def esperado_por_kwp(mes: int) -> tuple[float, str]:
    """
    kWh/kWp que um sistema SAUDÁVEL deveria fazer num dia deste mês.

    Deliberadamente NÃO usa o histórico da usina como referência: uma usina que
    sempre gerou pouco tem histórico baixo e seria aprovada por comparação
    consigo mesma. O histórico entra separado, como contexto — não como meta.
    """
    if os.environ.get("KWH_POR_KWP_ESPERADO", "").strip():
        return KWH_POR_KWP_ESPERADO, "valor fixo do .env"
    return FORMA_MENSAL[mes] * RENDIMENTO_SISTEMA, "referência regional"


def historico_por_kwp(mes: int, anual_por_kwp: float | None) -> float | None:
    """kWh/kWp que ESTA usina costuma fazer num dia deste mês, pelo ano passado."""
    if not anual_por_kwp or anual_por_kwp <= 0:
        return None
    return anual_por_kwp * FORMA_MENSAL[mes] / SOMA_PONDERADA


def classificar(atingido: float | None) -> tuple[str, str]:
    """(rótulo, ícone) a partir da fração do esperado que foi atingida."""
    if atingido is None:
        return "sem referência para comparar", "❔"
    if atingido >= 1.10:
        return f"{atingido:.0%} do esperado — acima", "🟢"
    if atingido >= 0.85:
        return f"{atingido:.0%} do esperado", "🟢"
    if atingido >= 0.60:
        return f"{atingido:.0%} do esperado — atenção", "🟡"
    if atingido <= 0.02:
        return "sem geração no dia", "🔴"
    return f"{atingido:.0%} do esperado — muito abaixo", "🔴"


# "dt" é o nome real do rótulo temporal na resposta da Nansen (confirmado
# 21/08/2026); os outros ficam como rede de segurança.
CAMPOS_DATA_SERIE = ["dt", "time", "date", "day", "statTime", "queryTime", "dateStr", "label"]

# Bits do dataItems: 6 = geração, 7 = comprado da rede, 8 = injetado,
# 11 = consumo da carga, 12 = autoconsumo. A resposta vem como uma LISTA de
# blocos, um por dataItem — misturar os blocos daria número errado, então a
# geração é escolhida explicitamente.
DATA_ITEM_GERACAO = 6
CAMPOS_VALOR_SERIE = [
    "generation",
    "pvGeneration",
    "energy",
    "generationEnergy",
    "value",
    "eDay",
    "power",
    "data",
]


def extrair_serie(rel: Any) -> list[tuple[str, float]]:
    """
    Tenta ler uma série (data -> kWh) de uma resposta de
    queryPlantReportByPlantId, cobrindo os formatos mais comuns do framework:

      A) {"dataList":[{"time":"2026-08-20","generation":123.4}, ...]}
      B) {"xAxis":["01","02",...], "series":[{"name":"...","data":[1,2,...]}]}
      C) lista solta de dicts no topo da resposta

    Devolve [] se não reconhecer nada — o chamador cai no plano B.
    """
    pontos: list[tuple[str, float]] = []

    # FORMATO REAL DA NANSEN (confirmado 21/08/2026):
    #   [{"dataItem": 6, "data": [{"dt": "2026-08-20", "value": 223.3}, ...]}, ...]
    # Um bloco por série. Pega só o bloco 6 (geração): juntar os blocos somaria
    # geração com energia comprada da rede.
    if isinstance(rel, list) and rel and all(isinstance(x, dict) for x in rel):
        if any("dataItem" in b for b in rel):
            bloco = next(
                (
                    b
                    for b in rel
                    if b.get("dataItem") == DATA_ITEM_GERACAO and isinstance(b.get("data"), list)
                ),
                None,
            )
            if bloco is None:
                itens = sorted({b.get("dataItem") for b in rel if "dataItem" in b} - {None})
                dbg(f"resposta sem o dataItem {DATA_ITEM_GERACAO} de geração; veio: {itens}")
                return []
            for p in bloco["data"]:
                if not isinstance(p, dict):
                    continue
                rotulo = pegar(p, CAMPOS_DATA_SERIE)
                valor = num(pegar(p, CAMPOS_VALOR_SERIE))
                # value: null é mês/ano sem dado — descartar, não virar zero
                if rotulo not in (None, "") and valor is not None:
                    pontos.append((str(rotulo), valor))
            return pontos

    # A / C — qualquer lista de dicts com um campo de data e um de valor
    def varrer(obj: Any) -> None:
        if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
            for item in obj:
                rotulo = pegar(item, CAMPOS_DATA_SERIE)
                valor = num(pegar(item, CAMPOS_VALOR_SERIE))
                if rotulo not in (None, "") and valor is not None:
                    pontos.append((str(rotulo), valor))
        elif isinstance(obj, dict):
            for v in obj.values():
                varrer(v)
        elif isinstance(obj, list):
            for v in obj:
                varrer(v)

    varrer(rel)
    if pontos:
        return pontos

    # B — eixo x paralelo a series[].data
    if isinstance(rel, dict):
        eixo = None
        for chave in ("xAxis", "xaxis", "categories", "times", "dates"):
            if isinstance(rel.get(chave), list):
                eixo = [str(x) for x in rel[chave]]
                break
        series = rel.get("series") or rel.get("seriesList")
        if eixo and isinstance(series, list):
            for s in series:
                dados = s.get("data") if isinstance(s, dict) else None
                if isinstance(dados, list) and len(dados) == len(eixo):
                    for rotulo, valor in zip(eixo, dados):
                        v = num(valor)
                        if v is not None:
                            pontos.append((rotulo, v))
                    break
    return pontos


def casa_dia(rotulo: str, dia: date) -> bool:
    """'2026-08-20', '20/08/2026', '2026-08-20 00:00', '20' -> True para 20/08."""
    r = str(rotulo).strip()
    if dia.isoformat() in r or dia.strftime("%d/%m/%Y") in r or dia.strftime("%Y/%m/%d") in r:
        return True
    so_digitos = r.strip().lstrip("0") or "0"
    return len(r.strip()) <= 2 and so_digitos == str(dia.day)


# Combinações de parâmetros para a série histórica, em ordem de tentativa.
# Necessário porque `timeType=2` — que pela documentação seria "mês" — devolveu
# uma série ANUAL na API real. Em vez de chutar de novo, o script tenta e
# verifica: a combinação certa é a que traz o dia pedido.
COMBOS_SERIE: list[tuple[str, Any, str]] = [
    ("timeType", 1, "%Y-%m-%d"),
    ("timeType", 1, "%Y-%m"),
    ("dateType", 1, "%Y-%m-%d"),
    ("dateType", 2, "%Y-%m-%d"),
    ("timeType", 3, "%Y-%m-%d"),
    ("timeType", 4, "%Y-%m-%d"),
    ("timeType", 5, "%Y-%m-%d"),
    ("timeType", 2, "%Y-%m"),
    ("timeType", 2, "%Y-%m-%d"),  # sabidamente anual: por último, só por garantia
]

_combo_serie: tuple[str, Any, str] | None = None  # a que funcionou, reusada
_logou_cru = False  # loga o JSON cru uma vez só, não uma por usina


def geracao_anual(api: "Auxsol", pid: str, ano_atual: int) -> float | None:
    """
    kWh gerados no último ano COMPLETO. É o que a série anual (a mesma que não
    servia para o dia) entrega bem — e serve para calibrar o esperado.
    O ano corrente é ignorado porque está pela metade.
    """
    try:
        rel = api.serie(pid, "timeType", 2, f"{ano_atual}-01-01")
    except Exception as e:
        dbg(f"histórico anual da usina {pid} falhou: {e}")
        return None

    anos: dict[int, float] = {}
    for rotulo, valor in extrair_serie(rel):
        texto = str(rotulo).strip()
        if len(texto) == 4 and texto.isdigit() and valor > 0:
            anos[int(texto)] = valor

    completos = sorted(a for a in anos if a < ano_atual)
    if not completos:
        return None
    # o mais recente completo: reflete o estado atual da usina melhor que a média
    return anos[completos[-1]]


def geracao_do_dia(api: "Auxsol", pid: str, dia: date) -> tuple[float | None, str, str]:
    """
    kWh gerados no dia `dia`. Procura a granularidade diária tentando as
    combinações de parâmetros até uma trazer a data pedida; guarda a vencedora
    para as usinas seguintes (senão seriam 9 chamadas por usina).

    Devolve (valor, origem, diagnóstico) — o diagnóstico explica POR QUE falhou,
    porque "não veio" não serve para depurar nada.
    """
    global _combo_serie, _logou_cru

    if not pid:
        return None, "", "usina sem plantId"

    ordem = ([_combo_serie] if _combo_serie else []) + [
        c for c in COMBOS_SERIE if c != _combo_serie
    ]
    ultimo = "nenhuma combinação de parâmetros trouxe a série"

    for parametro, valor, formato in ordem:
        tentativa = f"{parametro}={valor}, queryTime={dia.strftime(formato)}"
        try:
            rel = api.serie(pid, parametro, valor, dia.strftime(formato))
        except Exception as e:
            ultimo = f"a API recusou a série ({' '.join(str(e).split())[:90]})"
            continue

        serie = extrair_serie(rel)
        if not serie:
            ultimo = "a série veio em formato não reconhecido"
            if not _logou_cru:
                _logou_cru = True
                log(
                    f"::warning::série sem pontos com {tentativa}. "
                    f"JSON cru: {json.dumps(rel, ensure_ascii=False)[:1200]}"
                )
            continue

        for rotulo, v in serie:
            if casa_dia(rotulo, dia):
                if _combo_serie != (parametro, valor, formato):
                    _combo_serie = (parametro, valor, formato)
                    log(f"série diária obtida com {tentativa} — usando isso nas demais usinas")
                return v, "série da API", ""

        rotulos = [str(r) for r, _ in serie][:6]
        ultimo = f"a série de {tentativa} não traz {dia.strftime('%d/%m')} ({', '.join(rotulos)})"
        dbg(f"usina {pid}: {ultimo}")

    return None, "", ultimo


def analisar_usina(
    usina: dict,
    detalhe: Any,
    alarmes: list[dict],
    cap_cfg: dict,
    kwh_dia_forcado: float | None = None,
    anual_por_kwp: float | None = None,
    mes_ref: int | None = None,  # NÃO chamar de `mes`: colide com a geração do mês
) -> dict:
    nome = limpar_nome(str(pegar(usina, CAMPOS_NOME, "usina sem nome")))
    pid = str(pegar(usina, CAMPOS_ID, ""))

    kwp = primeiro(num(pegar(usina, CAMPOS_KWP)), num(pegar(detalhe, CAMPOS_KWP)))
    for chave, valor in cap_cfg.items():  # override manual do .env vence
        if chave and chave in sem_acento(nome):
            kwp = valor
            break
    # algumas contas devolvem capacidade em W
    if kwp and kwp > 5000:
        kwp = kwp / 1000.0

    if kwh_dia_forcado is not None:
        dia = kwh_dia_forcado
    else:
        dia = primeiro(num(pegar(detalhe, CAMPOS_DIA)), num(pegar(usina, CAMPOS_DIA)))
    mes = primeiro(num(pegar(detalhe, CAMPOS_MES)), num(pegar(usina, CAMPOS_MES)))
    total = primeiro(num(pegar(detalhe, CAMPOS_TOTAL)), num(pegar(usina, CAMPOS_TOTAL)))
    pot = primeiro(num(pegar(detalhe, CAMPOS_POT)), num(pegar(usina, CAMPOS_POT)))
    status_bruto = primeiro(pegar(detalhe, CAMPOS_STATUS), pegar(usina, CAMPOS_STATUS))

    horas_api = primeiro(num(pegar(detalhe, CAMPOS_HORAS)), num(pegar(usina, CAMPOS_HORAS)))
    atualizado = primeiro(pegar(detalhe, CAMPOS_DT), pegar(usina, CAMPOS_DT))
    tarifa = primeiro(num(pegar(usina, CAMPOS_TARIFA)), num(pegar(detalhe, CAMPOS_TARIFA)))

    # Se estamos usando o dado de ontem, o fullLoadHour da API (que é de hoje)
    # não serve: recalcula. Só aproveita o da API quando o dia é o mesmo.
    if dia is not None and kwp:
        rendimento = dia / kwp
    else:
        rendimento = horas_api if kwh_dia_forcado is None else None

    # Quanto se esperava deste dia neste mês, e quanto esta usina costuma fazer.
    o_mes = mes_ref or agora().month
    ref_por_kwp, base_ref = esperado_por_kwp(o_mes)
    hist_por_kwp = historico_por_kwp(o_mes, anual_por_kwp)
    esperado = (kwp * ref_por_kwp) if kwp else None
    historico = (kwp * hist_por_kwp) if (kwp and hist_por_kwp) else None
    atingido = (rendimento / ref_por_kwp) if (rendimento is not None and ref_por_kwp) else None
    rotulo, icone = classificar(atingido)

    economia = (dia * tarifa) if (dia is not None and tarifa) else None

    meus_alarmes = [
        a
        for a in alarmes
        if (pid and str(pegar(a, CAMPOS_ID, "")) == pid)
        or sem_acento(str(pegar(a, CAMPOS_NOME, ""))) == sem_acento(nome)
    ]
    if meus_alarmes:
        icone = "🔴"

    return {
        "id": pid,
        "nome": nome,
        "kwp": kwp,
        "kwh_dia": dia,
        "kwh_mes": mes,
        "kwh_total": total,
        "potencia_kw": pot,
        "status": STATUS_CONHECIDO.get(str(status_bruto), f"status {status_bruto}"),
        "atualizado": atualizado,
        "tarifa": tarifa,
        "economia_rs": economia,
        "kwh_por_kwp": rendimento,
        "kwh_esperado": esperado,
        "kwh_historico": historico,
        "atingido": atingido,
        "base_referencia": base_ref,
        "rotulo": rotulo,
        "icone": icone,
        "alarmes": meus_alarmes,
    }


def descrever_alarme(a: dict) -> str:
    desc = pegar(a, ["alarmName", "alarmContent", "content", "description", "message", "faultName"])
    nivel = pegar(a, ["alarmLevel", "level", "severity", "grade"])
    quando = pegar(a, ["alarmTime", "happenTime", "createTime", "startTime", "time"])
    partes = [str(desc or "alarme sem descrição")]
    if nivel not in (None, ""):
        partes.append(f"nível {nivel}")
    if quando not in (None, ""):
        partes.append(str(quando))
    return " · ".join(partes)


# --------------------------------------------------------------------------- #
# Gemini (opcional)
# --------------------------------------------------------------------------- #

PROMPT = """Você é analista de operação de usinas solares fotovoltaicas.
Escreva NO MÁXIMO 3 frases curtas, em português do Brasil, sobre o dia de ontem
nas usinas abaixo. Seja concreto: cite nome de usina e número quando ajudar.
Priorize o que exige ação (alarme ativo, usina sem geração, queda relevante).
Se tudo estiver normal, diga isso em uma frase e não invente problema.
Não use markdown, não use lista, não repita os números todos.

Referência de dia bom na região: {esperado} kWh por kWp instalado.

Dados:
{dados}
"""


def comentario_ia(analises: list[dict]) -> str:
    if not GEMINI_API_KEY:
        dbg("sem GEMINI_API_KEY; card sai sem comentário da IA")
        return ""

    resumo = [
        {
            "usina": a["nome"],
            "kwp_instalado": a["kwp"],
            "kwh_no_dia": a["kwh_dia"],
            "kwh_por_kwp": round(a["kwh_por_kwp"], 2) if a["kwh_por_kwp"] else None,
            "kwh_esperado": round(a["kwh_esperado"], 1) if a["kwh_esperado"] else None,
            "percentual_do_esperado": f"{a['atingido']:.0%}" if a["atingido"] else None,
            "kwh_que_esta_usina_costuma_fazer": round(a["kwh_historico"], 1)
            if a["kwh_historico"]
            else None,
            "kwh_no_mes": a["kwh_mes"],
            "economia_estimada_reais": round(a["economia_rs"], 2) if a["economia_rs"] else None,
            "ultima_leitura": a["atualizado"],
            "avaliacao": a["rotulo"],
            "alarmes_ativos": [descrever_alarme(x) for x in a["alarmes"]],
        }
        for a in analises
    ]
    prompt = PROMPT.format(
        esperado=KWH_POR_KWP_ESPERADO, dados=json.dumps(resumo, ensure_ascii=False, indent=1)
    )

    # o filter(None) evita tentar um modelo vazio se a variável vier em branco
    # gemini-2.0-flash foi aposentado (a própria API respondeu 404 indicando o
    # substituto). O "-latest" às vezes dá 503 por demanda, daí a lista.
    modelos = list(
        dict.fromkeys(
            filter(None, [GEMINI_MODEL, "gemini-flash-latest", "gemini-3.6-flash"])
        )
    )

    # Os modelos flash atuais "pensam" antes de responder, e o raciocínio consome
    # o mesmo orçamento de tokens da resposta: com 300 o texto saía cortado no
    # meio da frase. Orçamento maior + pedido para não gastar tokens pensando.
    # A segunda variante existe porque modelos antigos rejeitam thinkingConfig.
    variantes = [
        {"temperature": 0.2, "maxOutputTokens": 1200, "thinkingConfig": {"thinkingBudget": 0}},
        {"temperature": 0.2, "maxOutputTokens": 1200},
    ]

    for modelo in modelos:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{modelo}:generateContent?key={urllib.parse.quote(GEMINI_API_KEY)}"
        )
        for config in variantes:
            status, payload = http(
                url,
                "POST",
                corpo={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config},
                tentativas=2,
            )
            if status != 200:
                break  # erro do modelo: não insiste na outra variante
            texto = pegar(payload, ["text"])
            razao = str(pegar(payload, ["finishReason"], ""))
            if texto and razao != "MAX_TOKENS":
                return " ".join(str(texto).split())
            if razao == "MAX_TOKENS":
                log(f"aviso: Gemini ({modelo}) truncou a resposta; tentando outra configuração")
                continue
            break
        # a mensagem da própria API é o diagnóstico; só o status não diz nada
        motivo = _msg(payload) if isinstance(payload, dict) else str(payload)[:180]
        log(
            f"aviso: Gemini ({modelo}) respondeu HTTP {status}: "
            f"{' '.join(str(motivo).split())[:200]}"
        )
    log("aviso: card segue sem o comentário da IA. Rode 'testar-gemini' para diagnosticar.")
    return ""


# --------------------------------------------------------------------------- #
# Card v2 do Google Chat
# --------------------------------------------------------------------------- #


def fmt(v: float | None, sufixo: str = "", casas: int = 1) -> str:
    if v is None:
        return "—"
    s = f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s}{sufixo}"


def montar_card(
    analises: list[dict],
    comentario: str,
    avisos: list[str],
    dia: date,
    periodo: str = "",
    subtitulo: str = "",
) -> dict:
    total_dia = sum(a["kwh_dia"] or 0 for a in analises)
    total_kwp = sum(a["kwp"] or 0 for a in analises)
    total_rs = sum(a["economia_rs"] or 0 for a in analises)
    n_alarmes = sum(len(a["alarmes"]) for a in analises)
    rend_geral = (total_dia / total_kwp) if total_kwp else None
    total_esperado = sum(a["kwh_esperado"] or 0 for a in analises)
    # % geral só quando nenhuma usina está com dado parcial
    if total_esperado and not any(a.get("parcial") for a in analises):
        pct_geral: float | None = total_dia / total_esperado
    else:
        pct_geral = None

    if n_alarmes:
        titulo_icone = "🔴"
    elif any(a["icone"] == "🟡" for a in analises):
        titulo_icone = "🟡"
    elif all(a["icone"] == "⏳" for a in analises):
        titulo_icone = "⏳"
    else:
        titulo_icone = "🟢"

    secoes: list[dict] = [
        {
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": (periodo or f"Geração em {dia.strftime('%d/%m')}")
                        + f" — {len(analises)} usina(s)",
                        "text": f"<b>{fmt(total_dia, ' kWh')}</b>"
                        + (f"  ·  <b>R$ {fmt(total_rs)}</b>" if total_rs else "")
                        + (f"  ·  <b>{pct_geral:.0%}</b> do esperado" if pct_geral else ""),
                        "bottomLabel": (
                            f"esperado {fmt(total_esperado, ' kWh')} · " if total_esperado else ""
                        )
                        + f"{fmt(rend_geral, ' kWh/kWp', 2)} · "
                        f"{fmt(total_kwp, ' kWp', 0)} instalados · "
                        f"{n_alarmes} alarme(s) ativo(s)",
                    }
                }
            ]
        }
    ]

    if comentario:
        secoes.append(
            {
                "header": "Leitura do dia",
                "widgets": [{"textParagraph": {"text": comentario}}],
            }
        )

    for a in analises:
        linhas = [
            {
                "decoratedText": {
                    "topLabel": (a.get("data_numero") or dia).strftime("%d/%m")
                    + (" (parcial)" if a.get("parcial") else ""),
                    "text": f"<b>{fmt(a['kwh_dia'], ' kWh')}</b>  ·  {fmt(a['kwh_por_kwp'], ' kWh/kWp', 2)}",
                    "bottomLabel": (
                        # num dia em curso a comparação com o esperado ainda não
                        # fechou: dizer "abaixo do esperado" seria precipitado
                        f"parcial — {a['rotulo']} até agora"
                        if a.get("parcial")
                        else a["rotulo"]
                    )
                    + (f" · {a['origem_dia']}" if a.get("origem_dia") else ""),
                }
            },
            {
                "decoratedText": {
                    "topLabel": "Mês / Total",
                    "text": f"{fmt(a['kwh_mes'], ' kWh')}  ·  {fmt(a['kwh_total'], ' kWh')}",
                    "bottomLabel": f"{fmt(a['kwp'], ' kWp', 0)} instalados · {a['status']}"
                    + (
                        f" · última leitura {a['atualizado']}"
                        if a.get("atualizado")
                        else ""
                    ),
                }
            },
        ]
        extras: list[dict] = []

        if a["kwh_esperado"]:
            if a.get("parcial"):
                # dia em curso: mostra a meta, mas não a % (seria enganosa)
                texto = f"<b>{fmt(a['kwh_esperado'], ' kWh')}</b> é a meta do dia"
                rodape = f"{a['base_referencia']} · dia ainda em curso"
            else:
                texto = f"<b>{fmt(a['kwh_esperado'], ' kWh')}</b>"
                if a["atingido"] is not None:
                    texto += f"  ·  atingiu <b>{a['atingido']:.0%}</b>"
                rodape = a["base_referencia"]
                falta = a["kwh_esperado"] - (a["kwh_dia"] or 0)
                if a["atingido"] is not None and falta > 0:
                    rodape += f" · faltaram {fmt(falta, ' kWh')}"
            if a["kwh_historico"]:
                rodape += f" · média desta usina: {fmt(a['kwh_historico'], ' kWh')}"
            extras.append(
                {"decoratedText": {"topLabel": "Esperado", "text": texto, "bottomLabel": rodape}}
            )

        if a["economia_rs"] is not None:
            extras.append(
                {
                    "decoratedText": {
                        "topLabel": "Economia estimada",
                        "text": f"<b>R$ {fmt(a['economia_rs'])}</b>",
                        "bottomLabel": f"teto, a R$ {fmt(a['tarifa'], '', 2)}/kWh "
                        "cadastrado na plataforma",
                    }
                }
            )

        # ordem: geração do dia → esperado → economia → acumulados → alarmes
        linhas[1:1] = extras
        for al in a["alarmes"][:5]:
            linhas.append({"textParagraph": {"text": f"⚠️ {descrever_alarme(al)}"}})
        if len(a["alarmes"]) > 5:
            linhas.append(
                {"textParagraph": {"text": f"… e mais {len(a['alarmes']) - 5} alarme(s)."}}
            )
        secoes.append(
            {
                "header": f"{a['icone']} {a['nome']}",
                "widgets": linhas,
            }
        )

    if avisos:
        # Sem `collapsible`: no Google Chat, expandir uma seção de card postado
        # por webhook dispara um evento de interação que webhook não sabe
        # responder — e o Chat exibe "Webhook Bot não processou sua solicitação".
        secoes.append(
            {
                "header": "Avisos da coleta",
                "widgets": [{"textParagraph": {"text": "• " + "<br>• ".join(avisos)}}],
            }
        )

    return {
        "cardsV2": [
            {
                "cardId": f"solar-{dia.isoformat()}",
                "card": {
                    "header": {
                        "title": f"{titulo_icone} Relatório solar diário",
                        "subtitle": subtitulo or (dia.strftime("%d/%m/%Y") + " · Nansen Solar"),
                    },
                    "sections": secoes,
                },
            }
        ]
    }


def enviar_chat(payload: dict) -> None:
    if DRY_RUN or not GCHAT_WEBHOOK_URL:
        if not GCHAT_WEBHOOK_URL and not DRY_RUN:
            log("aviso: GCHAT_WEBHOOK_URL vazio — imprimindo o payload em vez de enviar")
        log(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    status, resp = http(GCHAT_WEBHOOK_URL, "POST", corpo=payload)
    if status == 200:
        log("card enviado ao Google Chat")
    else:
        raise RuntimeError(f"Google Chat recusou o card — HTTP {status}: {str(resp)[:400]}")


# --------------------------------------------------------------------------- #
# subcomandos
# --------------------------------------------------------------------------- #


def exigir(*nomes: str) -> None:
    faltando = [n for n in nomes if not os.environ.get(n, "").strip()]
    if faltando:
        raise SystemExit(
            "faltam variáveis de ambiente: "
            + ", ".join(faltando)
            + "\nCadastre-as em Settings > Secrets and variables > Actions do repositório."
        )


# aceita atributo com aspas simples, duplas ou sem aspas nenhuma
RE_SCRIPT = re.compile(
    r"""<(?:script|link)[^>]*?(?:src|href)\s*=\s*(?:["']([^"']+)["']|([^\s"'>]+))""", re.I
)
# "/prod-api", '/stage-api/', "/openapi" — segmentos de caminho contendo "api"
RE_PREFIXO = re.compile(r"""["'`](/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]*api[A-Za-z0-9_-]*)/?["'`]""")
# baseURL:"https://x/y" / VUE_APP_BASE_API
RE_BASEURL = re.compile(
    r"""(?:baseURL|BASE_API|baseApi|VUE_APP_BASE_API|apiBaseUrl)["'\s:=]{1,6}["'`]([^"'`]{1,200})["'`]""",
    re.I,
)
RE_ABSOLUTA = re.compile(r"""["'`](https?://[A-Za-z0-9.\-]+(?:/[A-Za-z0-9_\-./]*)?)["'`]""")

# Ruído comum que casa com as regexes mas não é prefixo de API. Comparado por
# "contém": nomes de DLL do Windows aparecem em pedaços variados nos bundles.
LIXO = ("api-ms-win", "/rapid", "graphapi", "/wapi", "mapi", "napi", "/uapi")


def sondar_frontend() -> list[str]:
    """
    Baixa o HTML do front-end, acha os bundles JavaScript e lê deles o prefixo
    real da API. É onde a resposta está de fato: o navegador precisa saber esse
    endereço, então ele está escrito no código que o navegador baixa.
    """
    achados: list[str] = []

    def registrar(base: str) -> None:
        base = base.rstrip("/")
        if base and base not in achados:
            achados.append(base)

    for front in FRONTENDS:
        status, html = http(front, "GET", tentativas=1, timeout=15)
        if status != 200 or not isinstance(html, str):
            log(f"  · {front} — HTML não veio (HTTP {status})")
            continue

        assets = []
        for com_aspas, sem_aspas in RE_SCRIPT.findall(html):
            src = com_aspas or sem_aspas
            if src.endswith(".js") or ".js?" in src:
                assets.append(urllib.parse.urljoin(front + "/", src))
        assets = list(dict.fromkeys(assets))
        log(f"  · {front} — {len(assets)} bundle(s) JavaScript para inspecionar")

        for asset in assets[:12]:
            status, js = http(asset, "GET", tentativas=1, timeout=20)
            if status != 200 or not isinstance(js, str):
                continue

            for valor in RE_BASEURL.findall(js):
                valor = valor.strip()
                if valor.startswith("http"):
                    registrar(valor)
                elif valor.startswith("/"):
                    registrar(urllib.parse.urljoin(front, valor))
                    registrar(HOST_PRINCIPAL + valor)

            for prefixo in set(RE_PREFIXO.findall(js)):
                p = prefixo.rstrip("/").lower()
                if len(p) > 40 or any(ruido in p for ruido in LIXO):
                    continue
                registrar(urllib.parse.urljoin(front, prefixo))
                registrar(HOST_PRINCIPAL + prefixo)

            for url in set(RE_ABSOLUTA.findall(js)):
                u = url.lower()
                nosso_dominio = "auxsol" in u or "nansen" in u
                parece_api = "api" in u or "/prod" in u
                if nosso_dominio and parece_api:
                    registrar(url)

    return achados


def cmd_descobrir_url() -> int:
    exigir("AUXSOL_APP_ID", "AUXSOL_APP_SECRET")

    extras = [a for a in sys.argv[2:] if a.startswith("http")]
    extras += [u.strip() for u in os.environ.get("URLS_EXTRA", "").split(",") if u.strip()]

    log("sondando o front-end da plataforma para achar o prefixo real da API…")
    try:
        descobertos = sondar_frontend()
    except Exception as e:
        descobertos = []
        log(f"  · sondagem falhou ({e}); seguindo com a lista fixa")
    if descobertos:
        log(f"\n{len(descobertos)} endereço(s) extraído(s) do JavaScript do front:")
        for d in descobertos:
            log(f"    {d}")
    else:
        log("  · nada extraído do front — usando só a lista fixa")

    candidatos = list(
        dict.fromkeys(
            ([BASE_URL] if BASE_URL else []) + extras + descobertos + CANDIDATOS_BASE
        )
    )

    log(f"\ntestando {len(candidatos)} candidato(s) com a credencial real…\n")
    achou: list[str] = []
    for base in candidatos:
        try:
            Auxsol(base, APP_ID, APP_SECRET).autenticar(rapido=True)
            log(f"  ✅ {base}  — AUTENTICOU")
            achou.append(base)
        except Exception as e:
            # o erro interessa; o HTML da página de erro, não.
            resumo = " ".join(str(e).split())[:160]
            log(f"  ❌ {base}  — {resumo}")

    if achou:
        log(f"\n>>> Use AUXSOL_BASE_URL={achou[0]}")
        if len(achou) > 1:
            log(f"    (também funcionaram: {', '.join(achou[1:])})")
        return 0

    log(
        "\nNenhum candidato autenticou.\n"
        "\nLeitura dos erros acima:\n"
        "  · 404 do nginx  = o host existe, o prefixo do caminho está errado\n"
        "  · 401/403       = ACHOU a API, mas a credencial foi recusada\n"
        "  · DNS/hostname  = esse endereço não existe\n"
        "\nPróximo passo, 30 segundos no navegador:\n"
        "  1. entre logado em https://www.auxsolcloud.com\n"
        "  2. F12 > aba Network > filtro Fetch/XHR > recarregue a página\n"
        "  3. clique em qualquer requisição e copie a URL completa\n"
        "  4. rode este comando de novo com urls_extra = https://HOST/PREFIXO\n"
    )
    return 1


def cmd_listar_usinas() -> int:
    exigir("AUXSOL_BASE_URL", "AUXSOL_APP_ID", "AUXSOL_APP_SECRET")
    api = Auxsol(BASE_URL, APP_ID, APP_SECRET)
    usinas = api.usinas()
    if not usinas:
        log("a conta não retornou nenhuma usina.")
        return 1
    log(f"{len(usinas)} usina(s):\n")
    for u in usinas:
        log(
            f"  plantId={pegar(u, CAMPOS_ID, '?')}  "
            f"nome={pegar(u, CAMPOS_NOME, '?')}  "
            f"kWp={pegar(u, CAMPOS_KWP, '?')}  "
            f"status={pegar(u, CAMPOS_STATUS, '?')}"
        )
    log("\nJSON cru da primeira usina (útil pra conferir nomes de campo):")
    log(json.dumps(usinas[0], ensure_ascii=False, indent=2)[:2000])
    log(f"\nPLANT_IDS={','.join(str(pegar(u, CAMPOS_ID, '')) for u in usinas)}")
    return 0


def cmd_alarmes() -> int:
    exigir("AUXSOL_BASE_URL", "AUXSOL_APP_ID", "AUXSOL_APP_SECRET")
    api = Auxsol(BASE_URL, APP_ID, APP_SECRET)
    al = api.alarmes()
    log(f"{len(al)} alarme(s)")
    log(json.dumps(al[:20], ensure_ascii=False, indent=2))
    return 0


def cmd_testar_webhook() -> int:
    exigir("GCHAT_WEBHOOK_URL")
    enviar_chat(
        {
            "cardsV2": [
                {
                    "cardId": "teste",
                    "card": {
                        "header": {
                            "title": "✅ Teste de webhook",
                            "subtitle": agora().strftime("%d/%m/%Y %H:%M"),
                        },
                        "sections": [
                            {
                                "widgets": [
                                    {
                                        "textParagraph": {
                                            "text": "Se você está lendo isso, o webhook do espaço "
                                            "está configurado corretamente."
                                        }
                                    }
                                ]
                            }
                        ],
                    },
                }
            ]
        }
    )
    return 0


def cmd_relatorio() -> int:
    exigir("AUXSOL_BASE_URL", "AUXSOL_APP_ID", "AUXSOL_APP_SECRET")
    avisos: list[str] = []
    api = Auxsol(BASE_URL, APP_ID, APP_SECRET)

    usinas = api.usinas()
    if PLANT_IDS:
        filtradas = [u for u in usinas if str(pegar(u, CAMPOS_ID, "")) in PLANT_IDS]
        if filtradas:
            usinas = filtradas
        else:
            avisos.append(
                f"PLANT_IDS={','.join(PLANT_IDS)} não casou com nenhuma usina da conta; "
                "usando todas as usinas visíveis."
            )
    if not usinas:
        raise RuntimeError(
            "a API não retornou nenhuma usina para esta credencial. "
            "Confirme com a Nansen se os 3 dataloggers estão sob esta conta."
        )

    try:
        alarmes = api.alarmes()
    except Exception as e:
        alarmes = []
        avisos.append(f"não foi possível ler os alarmes: {e}")

    # O card das 7h fala do dia fechado: ontem. Às 7h a geração de "hoje"
    # é praticamente zero e não diz nada.
    dia_ref = agora().date() - timedelta(days=1)

    analises: list[dict] = []
    cap_cfg = capacidades_configuradas()
    for u in usinas:
        pid = str(pegar(u, CAMPOS_ID, ""))
        detalhe: Any = {}
        if pid:
            try:
                detalhe = api.dados_atuais(pid)
            except Exception as e:
                avisos.append(f"sem dados em tempo real da usina {pid}: {e}")

        kwh_ontem, origem, diagnostico = geracao_do_dia(api, pid, dia_ref)

        # Calibra o esperado com o histórico da própria usina.
        kwp_cadastrado = num(pegar(u, CAMPOS_KWP))
        total_ano = geracao_anual(api, pid, dia_ref.year)
        anual_por_kwp = (total_ano / kwp_cadastrado) if (total_ano and kwp_cadastrado) else None

        a = analisar_usina(
            u,
            detalhe,
            alarmes,
            cap_cfg,
            kwh_dia_forcado=kwh_ontem,
            anual_por_kwp=anual_por_kwp,
            mes_ref=dia_ref.month,
        )
        a["origem_dia"] = origem
        a["parcial"] = False
        a["data_numero"] = dia_ref

        if kwh_ontem is None:
            if a["kwh_dia"] is not None:
                # Cai para o acumulado de HOJE — que é de outro dia e ainda por
                # fechar. O rótulo tem de mudar junto com o número, senão o card
                # anuncia ontem e mostra hoje.
                a["parcial"] = True
                a["data_numero"] = agora().date()
                a["origem_dia"] = f"parcial, até {agora().strftime('%H:%M')}"
                # Um dia em curso NÃO se compara com a expectativa de dia
                # fechado: às 7h daria "sem geração" nas três usinas, todo dia.
                # Alarme falso diário treina a equipe a ignorar o relatório.
                a["rotulo"] = "dia em curso, ainda sem julgar"
                a["icone"] = "🔴" if a["alarmes"] else "⏳"
                a["atingido"] = None  # % de um dia pela metade não significa nada
                avisos.append(f"{a['nome']}: {diagnostico}. Mostrando o parcial de hoje.")
            else:
                a["origem_dia"] = "sem dado"
                avisos.append(f"{a['nome']}: {diagnostico}. Também não há dado de hoje.")
        analises.append(a)

    # problema primeiro: quem precisa de ação aparece no topo do card.
    ordem_icone = {"🔴": 0, "🟡": 1, "⏳": 2, "❔": 3, "🟢": 4}
    analises.sort(key=lambda x: (ordem_icone.get(x["icone"], 9), sem_acento(x["nome"])))
    for a in analises:
        log(
            f"{a['icone']} {a['nome']}: "
            f"{(a.get('data_numero') or dia_ref).strftime('%d/%m')}"
            f"{' (parcial)' if a.get('parcial') else ''}="
            f"{fmt(a['kwh_dia'], ' kWh')} ({fmt(a['kwh_por_kwp'], ' kWh/kWp', 2)}) · "
            f"{a['rotulo']} · {len(a['alarmes'])} alarme(s)"
        )
    for aviso in avisos:  # os avisos também no log, não só no card
        log(f"::warning::{aviso}")

    # O título tem de descrever o que os números REALMENTE são.
    parciais = [a for a in analises if a.get("parcial")]
    if not parciais:
        periodo = f"Geração em {dia_ref.strftime('%d/%m')}"
        subtitulo = dia_ref.strftime("%d/%m/%Y") + " · dia fechado"
    elif len(parciais) == len(analises):
        hoje = agora()
        periodo = f"Parcial de hoje até {hoje.strftime('%H:%M')}"
        subtitulo = hoje.strftime("%d/%m/%Y") + " · DIA AINDA EM CURSO"
    else:
        periodo = "Geração — datas diferentes por usina"
        subtitulo = f"{dia_ref.strftime('%d/%m/%Y')} · leia a data de cada usina"

    card = montar_card(analises, comentario_ia(analises), avisos, dia_ref, periodo, subtitulo)
    enviar_chat(card)
    return 0


def cmd_sondar_serie() -> int:
    """
    Descobre qual combinação de parâmetros do queryPlantReportByPlantId devolve
    granularidade DIÁRIA. `timeType=2` devolveu anos, então o significado dos
    valores não é o que a documentação sugere — em vez de chutar, testa a matriz
    e mostra os rótulos que cada combinação retorna.
    """
    exigir("AUXSOL_BASE_URL", "AUXSOL_APP_ID", "AUXSOL_APP_SECRET")
    api = Auxsol(BASE_URL, APP_ID, APP_SECRET)

    pid = PLANT_IDS[0] if PLANT_IDS else str(pegar(api.usinas()[0], CAMPOS_ID, ""))
    ontem = (agora().date() - timedelta(days=1)).strftime("%Y-%m-%d")
    mes = ontem[:7]
    ano = ontem[:4]
    log(f"sondando a usina {pid}, procurando o dia {ontem}\n")

    combos: list[tuple[str, Any, str]] = []
    for valor in (1, 2, 3, 4, 5):
        combos.append(("timeType", valor, ontem))
    for valor in (1, 2, 3, 4):
        combos.append(("dateType", valor, ontem))
    for tempo in (mes, ano, ontem + " 00:00:00"):
        combos.append(("timeType", 2, tempo))

    achou: list[str] = []
    for chave, valor, tempo in combos:
        params = {
            "plantId": pid,
            chave: valor,
            "queryTime": tempo,
            "dataItems": DATA_ITEMS_ENERGIA,
        }
        try:
            rel = api.get("/analysis/plantReport/queryPlantReportByPlantId", params)
        except Exception as e:
            log(f"  {chave}={valor} queryTime={tempo:<22} ❌ {' '.join(str(e).split())[:80]}")
            continue

        serie = extrair_serie(rel)
        rotulos = [r for r, _ in serie]
        if not rotulos:
            itens = []
            if isinstance(rel, list):
                itens = sorted({b.get("dataItem") for b in rel if isinstance(b, dict)} - {None})
            log(f"  {chave}={valor} queryTime={tempo:<22} — sem pontos (dataItems: {itens})")
            continue

        # o que interessa: os rótulos parecem DIAS?
        diario = any(casa_dia(r, agora().date() - timedelta(days=1)) for r in rotulos)
        marca = "✅ DIÁRIO" if diario else "   "
        log(
            f"  {chave}={valor} queryTime={tempo:<22} {marca} "
            f"{len(rotulos)} ponto(s): {rotulos[:6]}"
        )
        if diario:
            achou.append(f"{chave}={valor} queryTime={tempo}")

    if achou:
        log(f"\n>>> Combinação que devolve dias: {achou[0]}")
        log("    Me mande esta linha e eu fixo no código.")
        return 0
    log(
        "\nNenhuma combinação devolveu granularidade diária.\n"
        "Nesse caso o relatório muda de estratégia: roda às 22h sobre o dia\n"
        "corrente (que a essa hora já está praticamente fechado) em vez de às 7h\n"
        "sobre o dia anterior. Os números passam a ser do próprio dia, honestos,\n"
        "e sem depender deste endpoint."
    )
    return 1


def cmd_testar_gemini() -> int:
    """Pergunta à própria API do Google quais modelos a chave pode usar."""
    exigir("GEMINI_API_KEY")
    chave = urllib.parse.quote(GEMINI_API_KEY)

    status, payload = http(
        f"https://generativelanguage.googleapis.com/v1beta/models?key={chave}",
        tentativas=2,
    )
    if status != 200:
        log(f"listar modelos falhou — HTTP {status}: {' '.join(str(_msg(payload)).split())[:300]}")
        log(
            "\nLeitura:\n"
            "  · 400/403  = chave inválida, revogada, ou sem a API habilitada\n"
            "  · 404      = endpoint errado (não deveria acontecer)\n"
            "  · HTTP 0   = a rede do runner não alcançou o Google\n"
            "Gere uma chave nova em https://aistudio.google.com/apikey e "
            "atualize o secret GEMINI_API_KEY."
        )
        return 1

    modelos = _dados(payload) or []
    uteis = []
    for m in modelos if isinstance(modelos, list) else []:
        nome = str(m.get("name", "")).replace("models/", "")
        metodos = m.get("supportedGenerationMethods") or []
        if "generateContent" in metodos:
            uteis.append(nome)

    log(f"a chave enxerga {len(uteis)} modelo(s) capaz(es) de generateContent:\n")
    for nome in uteis:
        log(f"    {nome}")

    preferidos = [n for n in uteis if "flash" in n and "thinking" not in n]
    escolhido = (preferidos or uteis or [""])[0]
    if not escolhido:
        log("\nNenhum modelo disponível para esta chave.")
        return 1

    log(f"\ntestando geração com '{escolhido}'…")
    status, payload = http(
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{escolhido}:generateContent?key={chave}",
        "POST",
        corpo={"contents": [{"parts": [{"text": "Responda apenas: ok"}]}]},
        tentativas=2,
    )
    if status == 200 and pegar(payload, ["text"]):
        log(f"  ✅ funcionou. Cadastre a Variable GEMINI_MODEL={escolhido}")
        return 0
    log(f"  ❌ HTTP {status}: {' '.join(str(_msg(payload)).split())[:300]}")
    return 1


COMANDOS = {
    "relatorio": cmd_relatorio,
    "sondar-serie": cmd_sondar_serie,
    "testar-gemini": cmd_testar_gemini,
    "descobrir-url": cmd_descobrir_url,
    "listar-usinas": cmd_listar_usinas,
    "alarmes": cmd_alarmes,
    "testar-webhook": cmd_testar_webhook,
}


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "relatorio"
    fn = COMANDOS.get(cmd)
    if not fn:
        log(f"comando desconhecido: {cmd}\ndisponíveis: {', '.join(COMANDOS)}")
        return 2
    try:
        return fn()
    except SystemExit:
        raise
    except Exception as e:
        log(f"::error::{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
