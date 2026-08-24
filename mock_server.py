#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API AUXSOL falsa + receptor de webhook, para testar o main.py sem credenciais.

  Terminal 1:  python mock_server.py            # sobe em http://127.0.0.1:8899
  Terminal 2:  AUXSOL_BASE_URL=http://127.0.0.1:8899/prod-api \
               AUXSOL_APP_ID=1 AUXSOL_APP_SECRET=2 \
               GCHAT_WEBHOOK_URL=http://127.0.0.1:8899/gchat \
               python main.py relatorio

Os números são sintéticos: Tijucas gera bem, Bairro Novo gera fraco e
Makiolka está parada e com um alarme ativo — dá para ver os três estados
do card de uma vez. Os SNs dos dataloggers são os reais dos 3 postos.

Também finge a PLANILHA (o Apps Script) em /planilha, para testar a gravação
do histórico sem tocar no Drive:

  HISTORICO_WEBHOOK_URL=http://127.0.0.1:8899/planilha
  HISTORICO_WEBHOOK_TOKEN=qualquer-coisa

E imita duas armadilhas reais da API da Nansen:

  · com timeType=2 a série vem por ANO, não por dia (foi a descoberta que
    mudou o desenho do projeto);
  · o Tijucas tem 2024 parcial e 2025 NULO — o caso que fazia o card anunciar
    "média de 40,1 kWh" para uma usina de 100 kWp.
"""

import calendar
import json
import os
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTA = int(os.environ.get("PORTA_MOCK", "8899"))

USINAS = [
    {
        "plantId": 1408,
        "plantName": "Posto Tijucas (Guest)",
        "capacity": 100.0,
        "status": "01",
        "dataloggerSn": "A012311030084010",
        "createTime": "2024-04-26 10:12:00",
        "meterFlag": 1,
        "perfil": "bom",
        # o caso venenoso: entrou em operação em 2024 (ano parcial) e 2025 veio
        # nulo. Sem descartar o ano de entrada, este 2024 vira "o histórico".
        "historico": {"2024": 41230.0, "2025": None},
    },
    {
        "plantId": 1348,
        "plantName": "Posto Bairro Novo (Guest)",
        "capacity": 75.0,
        "status": "01",
        "dataloggerSn": "A012311130984125",
        "createTime": "2023-09-14 08:30:00",
        "meterFlag": 1,
        "perfil": "fraco",
        "historico": {"2024": 54120.0, "2025": 57410.84},
    },
    {
        "plantId": 1460,
        "plantName": "Posto Makiolka (Guest)",
        "capacity": 75.0,
        "status": "01",
        "dataloggerSn": "A012311130950854",
        "createTime": "2024-01-30 15:02:00",
        "meterFlag": 1,
        "perfil": "parada",
        "historico": {"2024": None, "2025": 43012.5},
    },
]

RENDIMENTO = {"bom": 4.4, "fraco": 2.1, "parada": 0.0}

# Campos internos do mock, que a API real não devolveria.
INTERNOS = ("perfil", "historico")


def envelope(dados):
    return {"code": "AWX-0000", "msg": "操作成功", "data": dados}


def serie_mensal(usina):
    hoje = date.today()
    dias = calendar.monthrange(hoje.year, hoje.month)[1]
    base = usina["capacity"] * RENDIMENTO[usina["perfil"]]
    pontos = []
    for d in range(1, min(dias, hoje.day) + 1):
        variacao = 0.85 + 0.3 * ((d * 37) % 10) / 10.0  # pseudoaleatório estável
        pontos.append(
            {
                "time": f"{hoje.year:04d}-{hoje.month:02d}-{d:02d}",
                "generation": round(base * variacao, 2),
                "gridPurchase": round(base * 0.4, 2),
                "gridFeedIn": round(base * 0.3, 2),
                "loadConsumption": round(base * 1.1, 2),
                "selfConsumption": round(base * 0.7, 2),
            }
        )
    return pontos


def serie_anual(usina):
    """Como a API real responde a timeType=2: um ponto por ANO, nulos inclusive."""
    return [
        {"dt": ano, "value": valor}
        for ano, valor in sorted((usina.get("historico") or {}).items())
    ]


def atual(usina):
    base = usina["capacity"] * RENDIMENTO[usina["perfil"]]
    return {
        "plantId": usina["plantId"],
        "plantName": usina["plantName"],
        "capacity": usina["capacity"],
        "fullLoadHour": round(RENDIMENTO[usina["perfil"]] * 0.08, 2),
        "dt": f"{date.today().isoformat()} 06:45:12",
        "tariff": {"plantId": usina["plantId"], "fixPrice": 0.87, "priceType": "1"},
        "currentPower": round(usina["capacity"] * 0.05, 2) if usina["perfil"] != "parada" else 0.0,
        "todayYield": round(base * 0.08, 2),  # 7h da manhã: quase nada ainda
        "monthlyYield": round(base * date.today().day, 2),
        "totalYield": round(base * 420, 2),
        "status": usina["status"],
    }


ALARMES = [
    {
        "plantId": 1460,
        "plantName": "Posto Makiolka (Guest)",
        "alarmName": "Inversor sem comunicação",
        "alarmLevel": 3,
        "alarmTime": f"{date.today().isoformat()} 03:12:00",
        "deviceSn": "A012311130950854",
    }
]


# Planilha falsa: {aba: {chave: linha}}. Só em memória — reiniciar o mock zera.
PLANILHA = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _responder(self, obj, status=200):
        corpo = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _corpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        print(f"  mock  {self.command} {self.path}", flush=True)

    def do_POST(self):
        corpo = self._corpo()
        if self.path.startswith("/prod-api/auth/token"):
            if not corpo.get("app_id") or not corpo.get("app_secret"):
                return self._responder({"code": "AWX-1001", "msg": "credenciais ausentes"}, 200)
            return self._responder(envelope({"access_token": "token-de-teste", "expires_in": 43200}))
        if self.path.startswith("/gchat"):
            print("\n===== payload recebido no webhook =====")
            print(json.dumps(corpo, ensure_ascii=False, indent=2))
            print("======================================\n", flush=True)
            return self._responder({"name": "spaces/AAA/messages/BBB"})

        if self.path.startswith("/planilha"):
            # Imita o Apps Script: mesma regra de substituição por chave, para
            # que o teste local prove a idempotência antes de ir para o Drive.
            if not corpo.get("token"):
                return self._responder({"ok": False, "erro": "token invalido"})
            aba = corpo.get("aba") or "historico"
            chave = tuple(corpo.get("chave") or ["data", "plant_id"])
            guardadas = PLANILHA.setdefault(aba, {})
            inseridas = atualizadas = 0
            for linha in corpo.get("linhas") or []:
                k = tuple(str(linha.get(c, "")) for c in chave)
                if k in guardadas:
                    atualizadas += 1
                else:
                    inseridas += 1
                guardadas[k] = linha
            print(
                f"  planilha[{aba}] +{inseridas} ~{atualizadas} "
                f"(total {len(guardadas)})",
                flush=True,
            )
            return self._responder({
                "ok": True, "inseridas": inseridas,
                "atualizadas": atualizadas, "total": len(guardadas),
            })
        self._responder({"code": "AWX-9999", "msg": f"rota desconhecida: {self.path}"}, 404)

    def do_GET(self):
        p = self.path.split("?")[0]
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            return self._responder({"code": "AWX-4010", "msg": "token invalid"}, 401)

        if p == "/prod-api/archive/plant/list":
            return self._responder(
                envelope({
                    "total": len(USINAS),
                    "list": [
                        {k: v for k, v in u.items() if k not in INTERNOS} for u in USINAS
                    ],
                })
            )

        if p.startswith("/prod-api/analysis/plantReport/queryPlantCurrentData/"):
            pid = p.rsplit("/", 1)[-1]
            u = next((x for x in USINAS if str(x["plantId"]) == pid), None)
            if not u:
                return self._responder({"code": "AWX-4040", "msg": "usina inexistente"}, 200)
            return self._responder(envelope(atual(u)))

        if p == "/prod-api/analysis/plantReport/queryPlantCurrentDataAll":
            return self._responder(envelope([atual(u) for u in USINAS]))

        if p == "/prod-api/analysis/plantReport/queryPlantReportByPlantId":
            from urllib.parse import parse_qs, urlparse

            q = parse_qs(urlparse(self.path).query)
            pid = (q.get("plantId") or [""])[0] or str(self._corpo().get("plantId", ""))
            u = next((x for x in USINAS if str(x["plantId"]) == pid), None)
            if not u:
                return self._responder({"code": "AWX-4040", "msg": "usina inexistente"}, 200)
            # MOCK_SERIE permite ensaiar os desfechos ruins do card:
            #   ok (padrão) | ruim (formato irreconhecível) | erro (API recusa)
            modo = os.environ.get("MOCK_SERIE", "ok")
            if modo == "erro":
                return self._responder({"code": "AWX-5003", "msg": "sem permissão de convidado"}, 200)
            if modo == "ruim":
                return self._responder(envelope({"plantId": u["plantId"], "resumo": "sem série"}))

            # timeType=2 é o que o `geracao_anual` pede — e a API real responde
            # por ANO, com nulos. Devolver série diária aqui esconderia o bug.
            if (q.get("timeType") or [""])[0] == "2":
                return self._responder(envelope([{"dataItem": 6, "data": serie_anual(u)}]))

            # formato real da Nansen: uma lista de blocos, um por dataItem
            pontos = serie_mensal(u)
            return self._responder(envelope([
                {"dataItem": 6, "data": [{"dt": x["time"], "value": x["generation"]} for x in pontos],
                 "inverterYield": None, "weather": None, "astro": None},
                {"dataItem": 7, "data": [{"dt": x["time"], "value": x["gridPurchase"]} for x in pontos]},
                {"dataItem": 8, "data": [{"dt": x["time"], "value": x["gridFeedIn"]} for x in pontos]},
            ]))

        if p == "/prod-api/analysis/alarm/list":
            return self._responder(envelope({"total": len(ALARMES), "list": ALARMES}))

        self._responder({"code": "AWX-9999", "msg": f"rota desconhecida: {p}"}, 404)


if __name__ == "__main__":
    print(f"API falsa da AUXSOL em http://127.0.0.1:{PORTA}/prod-api")
    print(f"receptor de webhook em  http://127.0.0.1:{PORTA}/gchat")
    print(f"planilha falsa em       http://127.0.0.1:{PORTA}/planilha\n")
    ThreadingHTTPServer(("127.0.0.1", PORTA), Handler).serve_forever()
