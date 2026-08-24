#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
armazenamento.py — o histórico que a API não guarda.

POR QUE ESTE ARQUIVO EXISTE
---------------------------
A API da Nansen não tem série diária: `queryPlantReportByPlantId` devolve
totais por ano, e o único número de "hoje" é o `todayYield`, que é um
instantâneo — amanhã ele já é outro valor e o de hoje não volta mais.

Ou seja: **todo dia que este arquivo não roda é um dia de histórico perdido
para sempre.** É por isso que ele grava antes de qualquer dashboard existir.
Sem ele, qualquer ferramenta de BI só consegue mostrar o dia de hoje.

O QUE ELE GRAVA
---------------
Uma linha por usina por dia (a "tabela fato" do modelo do Power BI) e uma
linha por usina (a "tabela dimensão", reescrita a cada execução porque
capacidade e tarifa mudam raramente, mas mudam).

ONDE ELE GRAVA
--------------
Dois destinos, os dois opcionais, escolhidos por variável de ambiente:

  HISTORICO_CSV            caminho de um .csv local (ex: dados/historico.csv)
  HISTORICO_WEBHOOK_URL    URL de um Apps Script publicado como app web
  HISTORICO_WEBHOOK_TOKEN  segredo combinado com o Apps Script

O webhook é o caminho para a planilha do Google: o Power BI lê a planilha
pelo conector nativo, sem gateway e sem licença extra. Foi escolhido em vez
da API oficial do Google Sheets porque aquela exige assinar um JWT com RS256,
que precisaria de `pip install` — e este projeto é só biblioteca padrão.
Ver apps_script/Historico.gs.

Nada aqui pode derrubar o relatório: se o destino estiver fora do ar, a função
devolve um aviso e o card do Chat sai igual. Um histórico que quebra o
relatório diário é pior que um histórico faltando um dia.

REGRA DE GRAVAÇÃO: substitui, não empilha
-----------------------------------------
A chave é (data, plant_id). Regravar o mesmo dia SUBSTITUI a linha. Isso é de
propósito: o job pode rodar às 7h (parcial, dia em curso) e de novo às 22h
(dia fechado), e o que vale é a última leitura. Empilhar as duas dobraria a
geração de todo dia em qualquer soma do Power BI — o tipo de erro que ninguém
percebe até o número virar assunto numa reunião.
"""

from __future__ import annotations

import csv
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Iterable

# Ordem das colunas da tabela fato. Acrescentar no FIM: o Power BI aguenta
# coluna nova sem quebrar visual nenhum, mas renomear ou remover quebra todos.
COLUNAS_FATO = [
    "data",            # YYYY-MM-DD, dia local (America/Sao_Paulo)
    "plant_id",
    "usina",
    "kwp",
    "kwh_dia",
    "kwh_por_kwp",
    "kwh_esperado",
    "pct_atingido",    # kwh_dia / kwh_esperado, em fração (0,87 = 87%)
    "kwh_mes",
    "kwh_total",
    "tarifa_rs_kwh",
    "economia_dia_rs",
    "economia_mes_rs",
    "status",
    "parcial",         # 1 = lido com o dia ainda em curso; não julgar
    "alarmes",
    "base_referencia",
    "coletado_em",     # ISO local; desempata regravações do mesmo dia
]

COLUNAS_DIM = [
    "plant_id",
    "usina",
    "kwp",
    "tarifa_rs_kwh",
    "status",
    "atualizado_em",
    "coletado_em",
]

CHAVE_FATO = ("data", "plant_id")
CHAVE_DIM = ("plant_id",)

ABA_FATO = "historico"
ABA_DIM = "usinas"


# --------------------------------------------------------------------------- #
# montagem das linhas
# --------------------------------------------------------------------------- #


def _n(v: Any, casas: int = 3) -> Any:
    """Número arredondado, ou string vazia. Vazio ≠ zero: usina parada gera 0,0,
    que é dado; ausência de leitura é outra coisa e não pode virar zero."""
    if v is None or v == "":
        return ""
    try:
        return round(float(v), casas)
    except (TypeError, ValueError):
        return ""


def linha_fato(analise: dict, coletado_em: str) -> dict:
    """Uma linha da tabela fato a partir do dict que `analisar_usina` devolve."""
    data = analise.get("data_numero")
    return {
        "data": data.isoformat() if hasattr(data, "isoformat") else str(data or ""),
        "plant_id": str(analise.get("id") or ""),
        "usina": str(analise.get("nome") or ""),
        "kwp": _n(analise.get("kwp"), 2),
        "kwh_dia": _n(analise.get("kwh_dia"), 2),
        "kwh_por_kwp": _n(analise.get("kwh_por_kwp"), 3),
        "kwh_esperado": _n(analise.get("kwh_esperado"), 2),
        "pct_atingido": _n(analise.get("atingido"), 4),
        "kwh_mes": _n(analise.get("kwh_mes"), 2),
        "kwh_total": _n(analise.get("kwh_total"), 2),
        "tarifa_rs_kwh": _n(analise.get("tarifa"), 4),
        "economia_dia_rs": _n(analise.get("economia_rs"), 2),
        "economia_mes_rs": _n(analise.get("economia_mes_rs"), 2),
        "status": str(analise.get("status") or ""),
        "parcial": 1 if analise.get("parcial") else 0,
        "alarmes": len(analise.get("alarmes") or []),
        "base_referencia": str(analise.get("base_referencia") or ""),
        "coletado_em": coletado_em,
    }


def linha_dim(analise: dict, coletado_em: str) -> dict:
    return {
        "plant_id": str(analise.get("id") or ""),
        "usina": str(analise.get("nome") or ""),
        "kwp": _n(analise.get("kwp"), 2),
        "tarifa_rs_kwh": _n(analise.get("tarifa"), 4),
        "status": str(analise.get("status") or ""),
        "atualizado_em": str(analise.get("atualizado") or ""),
        "coletado_em": coletado_em,
    }


# --------------------------------------------------------------------------- #
# destino: CSV local
# --------------------------------------------------------------------------- #


def _ler_csv(caminho: str) -> list[dict]:
    if not os.path.exists(caminho):
        return []
    with open(caminho, "r", encoding="utf-8-sig", newline="") as f:
        # restkey/restval nomeados: sem eles, uma linha com campos a mais vira
        # a chave None no dict, e o None depois entra na lista de colunas e
        # quebra o sorted() com TypeError.
        leitor = csv.DictReader(f, restkey="_sobra", restval="")
        return [dict(r) for r in leitor]


def _gravar_csv(
    caminho: str, linhas: list[dict], colunas: list[str], chave: tuple[str, ...]
) -> str:
    antigas = _ler_csv(caminho)

    def k(r: dict) -> tuple:
        return tuple(str(r.get(c, "")) for c in chave)

    por_chave = {k(r): r for r in antigas}
    novas = sum(1 for r in linhas if k(r) not in por_chave)
    for r in linhas:
        por_chave[k(r)] = r

    # União das colunas: as conhecidas primeiro, na ordem canônica, e qualquer
    # coluna extra de um arquivo mais novo depois — assim uma versão antiga do
    # código nunca apaga coluna que uma versão nova gravou. Nomes que não são
    # texto (o "_sobra" de linha torta, por exemplo) ficam de fora.
    extras = [
        c
        for r in por_chave.values()
        for c in r
        if isinstance(c, str) and c and c != "_sobra" and c not in colunas
    ]
    campos = colunas + sorted(dict.fromkeys(extras))

    pasta = os.path.dirname(caminho)
    if pasta:
        os.makedirs(pasta, exist_ok=True)

    ordenadas = sorted(por_chave.values(), key=lambda r: k(r))

    # Gravação ATÔMICA: arquivo temporário e rename. A gravação reescreve o
    # arquivo inteiro, então um kill no meio (timeout do job, cancelamento,
    # runner preemptado) deixaria o histórico truncado num byte qualquer — e o
    # commit seguinte empurraria a versão truncada com mensagem normal.
    # O os.replace é atômico no mesmo diretório, no Windows e no Linux.
    temporario = f"{caminho}.tmp"
    with open(temporario, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        for r in ordenadas:
            w.writerow({c: r.get(c, "") for c in campos})
    os.replace(temporario, caminho)

    return (
        f"csv {caminho}: {len(linhas)} linha(s) gravada(s) "
        f"({novas} nova(s), {len(linhas) - novas} substituída(s)), "
        f"{len(ordenadas)} no total"
    )


# --------------------------------------------------------------------------- #
# destino: webhook do Apps Script -> planilha do Google
# --------------------------------------------------------------------------- #


TENTATIVAS_WEBHOOK = 3


def _gravar_webhook(url: str, token: str, aba: str, linhas: list[dict], chave: tuple[str, ...]) -> str:
    corpo = json.dumps(
        {"token": token, "aba": aba, "chave": list(chave), "linhas": linhas},
        ensure_ascii=False,
    ).encode("utf-8")

    # Repetir é seguro porque a gravação é idempotente pela chave: a segunda
    # tentativa atualiza a linha que a primeira inseriu. E vale repetir porque
    # este é o destino que o Power BI lê — um 500 passageiro do Google não pode
    # custar um dia de histórico que não volta.
    ultimo = ""
    for tentativa in range(1, TENTATIVAS_WEBHOOK + 1):
        # O Apps Script responde 302 para script.googleusercontent.com; o
        # urllib segue sozinho e converte para GET, que é exatamente o que o
        # Google espera — o doPost já rodou e gravou na primeira requisição.
        req = urllib.request.Request(
            url,
            data=corpo,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                bruto = r.read().decode("utf-8", "replace")
                status = r.status
        except Exception as e:
            ultimo = str(e)
            if tentativa < TENTATIVAS_WEBHOOK:
                time.sleep(2 * tentativa)
                continue
            raise RuntimeError(f"{ultimo} (após {TENTATIVAS_WEBHOOK} tentativas)")

        try:
            resp = json.loads(bruto)
        except json.JSONDecodeError:
            # Resposta em HTML normalmente é a tela de login do Google: a
            # implantação não está como "Qualquer pessoa".
            resp = {"bruto": " ".join(bruto.split())[:200]}

        if status != 200 or not resp.get("ok"):
            ultimo = f"HTTP {status}: {str(resp)[:300]}"
            if tentativa < TENTATIVAS_WEBHOOK and status >= 500:
                time.sleep(2 * tentativa)
                continue
            raise RuntimeError(ultimo)

        inseridas = resp.get("inseridas")
        atualizadas = resp.get("atualizadas")
        # CONFERÊNCIA OBRIGATÓRIA: `ok: true` não prova que algo foi escrito.
        # Um script implantado numa versão antiga pode não reconhecer o payload
        # e responder ok com zero linhas — semanas de gravação vazia sem que
        # nada fique vermelho.
        if not isinstance(inseridas, int) or not isinstance(atualizadas, int):
            raise RuntimeError(f"resposta sem contagem de linhas: {str(resp)[:200]}")
        if inseridas + atualizadas != len(linhas):
            raise RuntimeError(
                f"enviei {len(linhas)} linha(s) e a planilha confirmou "
                f"{inseridas + atualizadas} — implantação desatualizada do Apps Script?"
            )

        return f"planilha (aba {aba}): {inseridas} inserida(s), {atualizadas} atualizada(s)"

    raise RuntimeError(ultimo or "falha desconhecida")


# --------------------------------------------------------------------------- #
# ponto de entrada
# --------------------------------------------------------------------------- #


def destinos_configurados() -> list[str]:
    nomes = []
    if (os.environ.get("HISTORICO_CSV") or "").strip():
        nomes.append("csv")
    if (os.environ.get("HISTORICO_WEBHOOK_URL") or "").strip():
        nomes.append("planilha")
    return nomes


def gravar(analises: Iterable[dict], coletado_em: str) -> list[tuple[bool, str]]:
    """
    Grava fato e dimensão em todos os destinos configurados.

    Devolve [(ok, mensagem)]. O contrato é `ok=True` SÓ para gravação
    confirmada — qualquer outra coisa (nenhum destino, nenhuma linha com
    chave, falha de rede) vem com ok=False para o chamador avisar no card.
    A versão anterior devolvia só texto e o chamador procurava a palavra
    "falhou": bastava uma mensagem com outra redação para uma perda total de
    dado sair como relatório verde.

    NUNCA levanta: o relatório diário não pode falhar porque a planilha caiu.
    """
    analises = list(analises)
    if not analises:
        return [(False, "histórico: nada a gravar (nenhuma usina analisada)")]

    fatos = [linha_fato(a, coletado_em) for a in analises]
    dims = [linha_dim(a, coletado_em) for a in analises]

    # Linha sem data ou sem id não tem chave: gravar seria criar lixo que
    # depois aparece como usina fantasma no dashboard. Vale para as DUAS
    # tabelas — a dimensão é justamente onde a fantasma apareceria, no slicer.
    validos = [r for r in fatos if r["data"] and r["plant_id"]]
    dims = [r for r in dims if r["plant_id"]]
    descartados = len(fatos) - len(validos)

    msgs: list[tuple[bool, str]] = []
    if descartados:
        msgs.append(
            (False, f"histórico: {descartados} linha(s) sem data ou plant_id, descartada(s) "
                    "— confira se o nome do campo de id mudou na API")
        )
    if not validos:
        return msgs + [(False, "histórico: nenhuma linha com chave válida — NADA foi guardado")]

    caminho = (os.environ.get("HISTORICO_CSV") or "").strip()
    url = (os.environ.get("HISTORICO_WEBHOOK_URL") or "").strip()
    token = (os.environ.get("HISTORICO_WEBHOOK_TOKEN") or "").strip()

    if not caminho and not url:
        return msgs + [
            (False, "histórico: nenhum destino configurado — defina HISTORICO_CSV e/ou "
                    "HISTORICO_WEBHOOK_URL, senão o dia de hoje não é guardado em lugar nenhum")
        ]

    if caminho:
        base, ext = os.path.splitext(caminho)
        for rotulo, linhas, colunas, chave, alvo in (
            ("fato", validos, COLUNAS_FATO, CHAVE_FATO, caminho),
            ("dim", dims, COLUNAS_DIM, CHAVE_DIM, f"{base}_usinas{ext or '.csv'}"),
        ):
            try:
                msgs.append((True, _gravar_csv(alvo, linhas, colunas, chave)))
            except Exception as e:
                msgs.append((False, f"histórico: falhou gravar o {rotulo} em {alvo}: {e}"))

    if url:
        for aba, linhas, chave in ((ABA_FATO, validos, CHAVE_FATO), (ABA_DIM, dims, CHAVE_DIM)):
            try:
                msgs.append((True, _gravar_webhook(url, token, aba, linhas, chave)))
            except Exception as e:
                msgs.append((False, f"histórico: falhou gravar a aba {aba} na planilha: {e}"))

    return msgs


def testar_destinos(coletado_em: str) -> list[tuple[bool, str]]:
    """
    Grava uma linha marcada num destino de TESTE (aba `teste`, arquivo
    `*_teste.csv`) para conferir credencial, permissão e formato sem esperar o
    job da noite e sem encostar nos dados reais.
    """
    linhas = [
        {
            "data": "1900-01-01",
            "plant_id": "TESTE",
            "usina": "linha de teste — pode apagar",
            "kwh_dia": 1.5,
            "coletado_em": coletado_em,
        }
    ]

    msgs: list[tuple[bool, str]] = []
    caminho = (os.environ.get("HISTORICO_CSV") or "").strip()
    url = (os.environ.get("HISTORICO_WEBHOOK_URL") or "").strip()
    token = (os.environ.get("HISTORICO_WEBHOOK_TOKEN") or "").strip()

    if caminho:
        base, ext = os.path.splitext(caminho)
        alvo = f"{base}_teste{ext or '.csv'}"
        try:
            msgs.append((True, _gravar_csv(alvo, linhas, COLUNAS_FATO, CHAVE_FATO)))
        except Exception as e:
            msgs.append((False, f"falhou gravar o csv de teste em {alvo}: {e}"))

    if url:
        if not token:
            msgs.append((False, "HISTORICO_WEBHOOK_TOKEN vazio — o Apps Script vai recusar"))
        try:
            msgs.append((True, _gravar_webhook(url, token, "teste", linhas, CHAVE_FATO)))
        except Exception as e:
            msgs.append((False, f"falhou gravar na planilha (aba teste): {e}"))

    return msgs


# --------------------------------------------------------------------------- #
# teste offline (sem API, sem planilha): python armazenamento.py
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import datetime
    import tempfile

    hoje = datetime.date(2026, 8, 24)
    exemplo = [
        {
            "id": "1348", "nome": "Posto Bairro Novo", "kwp": 75.0, "kwh_dia": 341.2,
            "kwh_por_kwp": 4.549, "kwh_esperado": 258.0, "atingido": 1.3227,
            "kwh_mes": 4210.0, "kwh_total": 91234.5, "tarifa": 0.87,
            "economia_rs": 296.84, "economia_mes_rs": 3662.7, "status": "normal",
            "atualizado": "2026-08-24 17:36:00", "alarmes": [], "parcial": False,
            "base_referencia": "referência regional", "data_numero": hoje,
        },
        {
            "id": "1408", "nome": "Posto Tijucas", "kwp": 100.0, "kwh_dia": 156.0,
            "kwh_por_kwp": 1.56, "kwh_esperado": 344.0, "atingido": 0.4535,
            "kwh_mes": 1980.0, "kwh_total": 60111.0, "tarifa": 0.87,
            "economia_rs": 135.72, "economia_mes_rs": 1722.6, "status": "normal",
            "atualizado": "2026-08-24 17:36:00", "alarmes": [], "parcial": False,
            "base_referencia": "referência regional", "data_numero": hoje,
        },
        {  # usina sem leitura do dia: precisa gravar vazio, não zero
            "id": "1460", "nome": "Posto Makiolka", "kwp": 75.0, "kwh_dia": None,
            "kwh_por_kwp": None, "kwh_esperado": 258.0, "atingido": None,
            "kwh_mes": 2870.0, "kwh_total": 44500.0, "tarifa": 0.87,
            "economia_rs": None, "economia_mes_rs": 2496.9, "status": "normal",
            "atualizado": "", "alarmes": [{"x": 1}], "parcial": True,
            "base_referencia": "referência regional", "data_numero": hoje,
        },
    ]

    pasta = tempfile.mkdtemp()
    os.environ["HISTORICO_CSV"] = os.path.join(pasta, "dados", "historico.csv")
    os.environ.pop("HISTORICO_WEBHOOK_URL", None)

    def rodar(rotulo, quando):
        print(rotulo)
        resultado = gravar(exemplo, quando)
        for ok, m in resultado:
            print(f"   {'ok  ' if ok else 'FALHA'} {m}")
        return resultado

    rodar("--- primeira gravação ---", "2026-08-24T22:05:00-03:00")

    exemplo[0]["kwh_dia"] = 355.0
    rodar("--- regravação do mesmo dia (deve SUBSTITUIR, não empilhar) ---",
          "2026-08-24T22:40:00-03:00")

    for a in exemplo:
        a["data_numero"] = datetime.date(2026, 8, 25)
    rodar("--- dia seguinte ---", "2026-08-25T22:05:00-03:00")

    print("\n--- conteúdo final ---")
    with open(os.environ["HISTORICO_CSV"], encoding="utf-8") as f:
        print(f.read())

    linhas = _ler_csv(os.environ["HISTORICO_CSV"])
    assert len(linhas) == 6, f"esperava 6 linhas (3 usinas × 2 dias), veio {len(linhas)}"
    bn = [r for r in linhas if r["plant_id"] == "1348" and r["data"] == "2026-08-24"]
    assert len(bn) == 1, "regravação empilhou em vez de substituir"
    assert bn[0]["kwh_dia"] == "355.0", f"ficou com o valor antigo: {bn[0]['kwh_dia']}"
    mk = [r for r in linhas if r["plant_id"] == "1460" and r["data"] == "2026-08-24"][0]
    assert mk["kwh_dia"] == "", "leitura ausente virou zero — erraria qualquer média"

    # Sem destino nenhum, o resultado tem de vir marcado como FALHA: é o caso
    # em que o dia se perde por completo, e ele não pode passar por sucesso.
    salvo = os.environ.pop("HISTORICO_CSV")
    assert all(not ok for ok, _ in gravar(exemplo, "x")), "sem destino saiu como sucesso"

    # Usina sem plant_id não pode virar linha nem no fato nem na dimensão.
    os.environ["HISTORICO_CSV"] = salvo
    sem_id = [dict(exemplo[0], id="")]
    resultado = gravar(sem_id, "2026-08-26T22:00:00-03:00")
    assert all(not ok for ok, _ in resultado), "linha sem id saiu como sucesso"
    assert not os.path.exists(salvo.replace(".csv", "_x.csv"))
    dims = _ler_csv(salvo.replace("historico.csv", "historico_usinas.csv"))
    assert all(r["plant_id"] for r in dims), "usina fantasma entrou na dimensão"

    # Linha torta no CSV não pode derrubar a gravação nem inventar coluna.
    with open(salvo, "a", encoding="utf-8") as f:
        f.write("2026-08-27,9999,Torta,1,2,3,LIXO,LIXO2,LIXO3\n")
    resultado = gravar(exemplo, "2026-08-28T22:00:00-03:00")
    assert any(ok for ok, _ in resultado), f"linha torta derrubou a gravação: {resultado}"
    with open(salvo, encoding="utf-8") as f:
        cabecalho = f.readline().strip().split(",")
    assert cabecalho == COLUNAS_FATO, f"cabeçalho contaminado: {cabecalho}"

    print("ok: substituição, ausência ≠ zero, sem-destino é falha, "
          "fantasma bloqueada, linha torta tolerada")
