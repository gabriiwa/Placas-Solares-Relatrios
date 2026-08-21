# Relatório solar diário — Nansen Solar / AUXSOL → Google Chat

Todo dia, no horário agendado, o GitHub Actions puxa a geração dos três postos na
API da Nansen (plataforma AUXSOL), compara com o que se esperava para a época do
ano, lê os alarmes ativos e manda um card para um espaço do Google Chat.

Custo zero: roda no plano gratuito do GitHub Actions (uns 3 min/mês) e usa só a
biblioteca padrão do Python — não há `pip install`, não há `requirements.txt`,
não há servidor para manter.

## Arquivos

| Arquivo | Para que serve |
|---|---|
| `main.py` | Todo o programa: cliente da API, cálculos, card, envio |
| `.github/workflows/daily_report.yml` | O agendamento e a execução manual |
| `mock_server.py` | API falsa, para testar sem credencial nenhuma |
| `.env.example` | Referência das variáveis |

## Configuração

**Secrets** (Settings → Secrets and variables → Actions → aba Secrets):

| Secret | Valor |
|---|---|
| `AUXSOL_BASE_URL` | `https://eu.auxsolcloud.com/auxsol-api` |
| `AUXSOL_APP_ID` | o app_id da credencial |
| `AUXSOL_APP_SECRET` | o app_secret |
| `GCHAT_WEBHOOK_URL` | webhook do espaço do Chat |
| `GEMINI_API_KEY` | opcional — só para o comentário em linguagem natural |

**Variables** (aba Variables, nada aqui é sigiloso):

| Variable | Quando usar |
|---|---|
| `PLANT_IDS` | não criar — vazio significa "todas as usinas da credencial" |
| `CAPACIDADE_KWP` | **não criar** — a API informa a capacidade certa; um valor manual errado estragaria a avaliação |
| `KWH_POR_KWP_ESPERADO` | só se quiser um esperado fixo, ignorando a sazonalidade |
| `HORA_DIA_FECHADO` | hora local a partir da qual o dia é julgado (padrão 20) |
| `GEMINI_MODEL` | se quiser fixar um modelo, ex. `gemini-flash-latest` |

Para criar o webhook: Google Chat → o espaço → nome do espaço → **Apps e
integrações** → **Webhooks** → **Adicionar webhook**. Copie a URL; ela é uma
credencial (quem a tem posta no espaço), então vai em Secret.

## Comandos

Pelo menu de **Run workflow**, no campo "Subcomando do main.py":

| Comando | O que faz |
|---|---|
| `relatorio` | o padrão: monta e envia o card |
| `listar-usinas` | imprime plantId, nome e kWp de cada usina, e o JSON cru da primeira |
| `alarmes` | imprime os alarmes crus |
| `testar-webhook` | manda um card de teste ao Chat, sem tocar na API da Nansen |
| `descobrir-url` | testa candidatos de `AUXSOL_BASE_URL`, se ela mudar |
| `testar-gemini` | lista os modelos que a chave enxerga e testa um |

As caixas `dry_run` (imprime o card no log em vez de enviar) e `debug` (loga cada
chamada) funcionam com qualquer comando.

Se um comando não aparecer no menu, é porque a lista de opções está escrita no
`.github/workflows/daily_report.yml` — acrescente a linha lá.

## O que o card mostra

Por usina, na ordem: geração do dia, **esperado e % atingido**, economia no dia,
economia no mês, acumulados, e cada alarme ativo. As usinas com problema vêm no
topo, e o título do card carrega o pior estado entre elas.

O semáforo compara a geração com o esperado do mês: 🟢 acima de 85%, 🟡 entre 60%
e 85%, 🔴 abaixo de 60% ou sem geração, e 🔴 sempre que houver alarme ativo.
⏳ significa "dia ainda em curso, não julgado".

### De onde vem o "esperado"

Da irradiação típica da região de Curitiba, mês a mês (tabela `FORMA_MENSAL` no
código), multiplicada por um rendimento de sistema de 0,80 e pela capacidade
instalada. Por isso a meta de uma usina de 75 kWp é 198 kWh em junho e 324 kWh em
dezembro — comparar o inverno com o verão faria o inverno parecer defeito.

**Não** é o histórico da própria usina, de propósito. Uma usina que sempre gerou
pouco tem histórico baixo e passaria a ser aprovada por comparação consigo mesma:
a Makiolka gerou 765 kWh/kWp em 2025, quando um sistema saudável em Curitiba faz
1.300–1.450, e contra o próprio histórico ela tirava 🟢 148%. O histórico aparece
ao lado, como contexto ("média desta usina: 153 kWh"), não como meta.

Os valores da tabela são típicos da região, não medidos no telhado. Depois de
alguns meses de dados reais vale calibrá-los.

### Sobre a economia em R$

Vem da tarifa cadastrada na própria plataforma (`tariff.fixPrice`, hoje
R$ 0,87/kWh) multiplicada pela geração. É um **teto**: vale cheio para o kWh
autoconsumido, que evita compra na tarifa cheia, mas o excedente injetado gera
crédito que, pela Lei 14.300/2022, não compensa 100% da TUSD e vale menos. Para
posto de combustível o autoconsumo é alto (carga no horário comercial coincide
com a geração), então o teto fica perto do real — mas não é o real. Conferir os
R$ 0,87 contra uma fatura.

A economia no mês é a mais útil das duas: é o número que se compara com a parcela
do financiamento, e sai de `monthlyYield`, que a API entrega de forma confiável.

### Sobre o dia e o horário

O relatório fala do **dia corrente**. A API não tem série histórica por dia — o
endpoint `queryPlantReportByPlantId` só devolve totais por **ano**, o que foi
verificado testando 9 combinações de parâmetros. Logo, "a geração de ontem" é um
dado que não existe nesta API.

Consequência prática: **o relatório precisa rodar depois do pôr do sol.** Antes
de `HORA_DIA_FECHADO` (padrão 20h) o card sai com ⏳, mostrando a meta do dia mas
sem julgar nada — porque às 7h da manhã a geração do dia é zero, e um relatório
que acusa "sem geração" todo dia de manhã é um relatório que ninguém lê no
terceiro dia.

O cron do workflow está em UTC. `0 1 * * *` = 22h de Brasília (o Brasil não tem
mais horário de verão, então não muda ao longo do ano).

## Testar na sua máquina, sem credencial

```bash
python mock_server.py                       # terminal 1

export AUXSOL_BASE_URL=http://127.0.0.1:8899/prod-api   # terminal 2
export AUXSOL_APP_ID=1 AUXSOL_APP_SECRET=2
export GCHAT_WEBHOOK_URL=http://127.0.0.1:8899/gchat
python main.py relatorio
```

O mock devolve dados sintéticos com os três estados de propósito e imprime o card
que teria ido ao Chat. `DRY_RUN=1` imprime em vez de enviar; `DEBUG=1` loga cada
chamada; `MOCK_SERIE=ruim|erro` ensaia as falhas da API; `PORTA_MOCK` troca a
porta.

## Problemas comuns

| Sintoma no log | Causa e correção |
|---|---|
| `can't open file 'main.py'` | o `main.py` não está na raiz do repositório |
| `Secrets ausentes: ...` | falta cadastrar em Settings → Secrets and variables |
| `SyntaxError: invalid decimal literal` na linha 1 | log colado dentro do `main.py`; suba o arquivo por **Upload files**, não copiando texto |
| `falha na autenticação — HTTP 404` | `AUXSOL_BASE_URL` errada; rode `descobrir-url` |
| `HTTP 401/403` | credencial recusada |
| `a API não retornou nenhuma usina` | a credencial autentica mas não vê os postos |
| `HTTP 429 Request frequency too high` | limite da Nansen; o script já espaça as chamadas (5 por 5 s, 0,7 s entre elas) |
| `Google Chat recusou o card — HTTP 404` | webhook apagado ou URL truncada (tem `key=` **e** `token=`) |
| card sem o texto da IA | sem `GEMINI_API_KEY`, ou 503 do modelo (temporário) — os números não dependem da IA |
| relatório atrasa 10–20 min | normal: o agendador do GitHub atrasa em horários cheios |

## Notas técnicas

- **Limite de requisições.** A Nansen documenta 10 req/5 s por IP, mas com 9
  tomamos 429 — o runner do GitHub compartilha IP de saída. O script usa 5 por
  5 s com 0,7 s de intervalo mínimo. Uma execução com 3 usinas faz ~8 chamadas.
- **Nomes dos campos.** Confirmados contra a API real: `todayYield`,
  `monthlyYield`, `totalYield`, `capacity`, `currentPower`, `fullLoadHour`,
  `dt`, `tariff.fixPrice`, `status` (string `"01"`). A busca é tolerante: cada
  dado é procurado por uma lista de nomes possíveis, e o que falta vira `—` no
  card com aviso, em vez de derrubar o relatório.
- **Zero é dado, não ausência.** Usina parada gera 0,0 kWh, e `or` em Python
  descartaria isso — daí o helper `primeiro()`.
- **Credencial de convidado.** `isVisitor: true`, nomes com "(Guest)" (o card
  limpa o sufixo). Basta para ler. Se o comando `alarmes` vier sempre vazio com
  usinas em operação, pode ser restrição de permissão, não ausência de alarme —
  vale confirmar com a Nansen, porque um relatório que nunca acusa problema cria
  confiança falsa.
- **Cálculos em Python, nunca na IA.** Eficiência, % do esperado, semáforo,
  economia e totais são calculados no código. A IA só escreve o comentário, e
  quando falha o card sai completo sem ele.
- **Se o relatório falhar**, o próprio workflow posta um aviso no espaço do Chat
  com o link do log.
