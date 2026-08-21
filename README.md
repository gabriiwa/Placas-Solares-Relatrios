# Relatório solar diário — Nansen Solar / AUXSOL → Google Chat

Todo dia às 7h (horário de Brasília) o GitHub Actions puxa a geração do dia
anterior dos três postos na API da Nansen Solar (plataforma AUXSOL), avalia a
eficiência, lê os alarmes ativos e manda um card para um espaço do Google Chat.

Custo: zero. Roda no plano gratuito do GitHub Actions (~3 min/mês de uso) e usa
só a biblioteca padrão do Python — não há `pip install`, não há
`requirements.txt`, não há servidor para manter.

## Arquivos

| Arquivo | Para que serve |
|---|---|
| `main.py` | Todo o programa: cliente da API, cálculos, card, envio |
| `.github/workflows/daily_report.yml` | O agendamento das 7h e a execução manual |
| `mock_server.py` | API falsa, para testar sem credencial nenhuma |
| `.env.example` | Referência das variáveis (não versione o `.env` real) |

## Por que a execução anterior falhou

Dois erros, os dois esperados nesse ponto:

1. **`can't open file 'main.py': No such file or directory`** — o workflow
   existia no repositório, mas o `main.py` não. O Actions subiu a máquina, fez o
   checkout e não achou o script. Resolve-se comitando o `main.py` na **raiz** do
   repositório (passo 1 abaixo).
2. **`PLANT_ID` vazio** — os secrets ainda não estavam cadastrados. Só que
   `PLANT_IDS` **não é obrigatório**: vazio significa "todas as usinas visíveis
   para esta credencial", que é justamente o que queremos. O que é obrigatório é
   `AUXSOL_BASE_URL`, `AUXSOL_APP_ID`, `AUXSOL_APP_SECRET` e
   `GCHAT_WEBHOOK_URL`.

## Passo 1 — subir os arquivos

Na página do repositório: **Add file → Upload files**, arraste o `main.py`, o
`mock_server.py` e o `README.md` para a raiz, e commite. O
`.github/workflows/daily_report.yml` substitui o workflow que já está lá (mesmo
caminho, mesmo nome de arquivo).

Estrutura final:

```
main.py
mock_server.py
README.md
.env.example
.github/workflows/daily_report.yml
```

## Passo 2 — criar o webhook do espaço no Google Chat

O webhook é o endereço para onde o card é enviado. Requer conta Google
Workspace (o `@grupotrapezio.com.br` serve; conta `@gmail.com` pessoal não tem
webhook de espaço).

1. Abra o Google Chat e crie (ou escolha) o espaço que vai receber o relatório —
   algo como "Solar — postos".
2. Clique no **nome do espaço** no topo → **Apps e integrações**.
3. **Webhooks** (ou "Gerenciar webhooks") → **Adicionar webhook**.
4. Nome: `Relatório Solar`. Avatar: opcional.
5. Copie a URL gerada. Ela começa com
   `https://chat.googleapis.com/v1/spaces/.../messages?key=...&token=...`

Essa URL **é uma credencial**: quem tem ela consegue postar no espaço. Vai em
Secret, nunca no código.

> Se o item "Webhooks" não aparecer, o administrador do Workspace desativou
> webhooks de entrada — é preciso pedir a liberação para o TI.

## Passo 3 — cadastrar os secrets

No repositório: **Settings → Secrets and variables → Actions**.

Na aba **Secrets** (`New repository secret`), os quatro obrigatórios:

| Secret | Valor |
|---|---|
| `AUXSOL_BASE_URL` | a URL base da API, ex. `https://www.auxsolcloud.com/prod-api` |
| `AUXSOL_APP_ID` | `119348` (credencial da conta Beatriz Dias) |
| `AUXSOL_APP_SECRET` | o app_secret da mesma credencial |
| `GCHAT_WEBHOOK_URL` | a URL do passo 2 |

E o opcional:

| Secret | Valor |
|---|---|
| `GEMINI_API_KEY` | chave do Google AI Studio (`aistudio.google.com/apikey`) |

Sem `GEMINI_API_KEY` o card sai exatamente igual, só sem o parágrafo em
linguagem natural. Todos os números vêm do Python, não da IA — a chave é um
extra, não uma dependência.

Na aba **Variables** (`New repository variable`), o que não é sigiloso:

| Variable | Valor sugerido |
|---|---|
| `PLANT_IDS` | deixe **sem criar** por enquanto (vazio = todas as usinas) |
| `CAPACIDADE_KWP` | `Tijucas=60,Bairro Novo=60,Makiolka=60` |
| `KWH_POR_KWP_ESPERADO` | `4.2` |

## Passo 4 — descobrir a URL base (a pendência que ainda bloqueia)

A documentação da AUXSOL não informa o host: diz para pedir ao suporte. Sem ele
nada funciona. Três caminhos, do mais rápido ao mais lento:

**a) Deixar o próprio Actions testar.** Cadastre `AUXSOL_APP_ID` e
`AUXSOL_APP_SECRET`, ponha qualquer coisa em `AUXSOL_BASE_URL`, vá em **Actions →
Relatório solar diário → Run workflow**, escolha o comando `descobrir-url` e
rode. O log diz qual candidato autenticou.

**b) Ler do navegador (o mais confiável).** Faça login na plataforma web da
Nansen/AUXSOL, aperte `F12` → aba **Network** → filtro **Fetch/XHR** → recarregue
a página. Clique em qualquer requisição da lista: a URL base é tudo o que vem
antes de `/auth/` ou `/analysis/`. Exemplo: se aparecer
`https://xxx.com/prod-api/analysis/plantReport/...`, a base é
`https://xxx.com/prod-api`.

**c) Pedir ao suporte da Nansen.** O e-mail enviado pediu documentação,
autenticação e limites, mas não pediu a URL. Vale um follow-up curto:

> Solicitamos também a **URL base (host) da API** para as chamadas — a
> documentação indica que esse endereço deve ser obtido junto ao suporte.
> Como exemplo do que precisamos: o endereço completo do endpoint `/auth/token`.
> Aproveitando, existe ambiente de homologação? E o limite de 10 requisições
> por 5 segundos por IP vale também para integrações de terceiros?

## Passo 5 — conferir as usinas

Com a URL base certa, rode o workflow manualmente com o comando
`listar-usinas`. O log imprime `plantId`, nome e kWp de cada usina, o JSON cru da
primeira (útil para conferir os nomes dos campos) e uma linha pronta para colar
em `PLANT_IDS`.

É aqui que se responde a dúvida em aberto do projeto: **se as três usinas
aparecerem**, a credencial já cobre tudo e não é preciso esperar liberação
nenhuma. Se aparecerem menos de três, os dataloggers estão sob outra conta e a
liberação continua pendente.

Dataloggers dos três postos, para conferência:

| Posto | Datalogger SN | Inversor |
|---|---|---|
| Tijucas | A012311030084010 | ASN-60TL-LV |
| Bairro Novo | A012311130984125 | ASN-60TL-LV |
| Makiolka | A012311130950854 | ASN-60TL-LV |

## Passo 6 — primeiro envio

**Actions → Relatório solar diário → Run workflow**:

- `comando: testar-webhook` — confirma que o card chega no espaço do Chat.
- `comando: relatorio` + `dry_run: true` — monta o card de verdade e só imprime
  no log, sem postar. Bom para revisar os números antes de mostrar para a equipe.
- `comando: relatorio` — para valer.

Depois disso o agendamento das 7h roda sozinho. Se algum dia falhar, o workflow
posta um aviso no próprio espaço com o link do log.

## O que o card mostra

- **Resumo** — geração total do dia anterior, kWh por kWp e alarmes ativos.
- **Leitura do dia** — 3 frases da IA, quando há `GEMINI_API_KEY`.
- **Uma seção por usina**, com quem está com problema no topo:
  - geração do dia e o rendimento em kWh/kWp;
  - acumulado do mês e total, capacidade instalada, potência instantânea;
  - cada alarme ativo, com nível e horário.
- **Avisos da coleta** — seção recolhida, aparece quando algum dado não veio.

O semáforo compara o rendimento do dia com `KWH_POR_KWP_ESPERADO`:
🟢 acima de 75% do esperado, 🟡 entre 50% e 75%, 🔴 abaixo de 50%, sem geração,
ou com alarme ativo.

**Sobre o "dia":** o card das 7h fala do **dia anterior**, fechado. Às 7h a
geração de hoje é praticamente zero e não diz nada. O número sai da série mensal
da API; se ela não vier, o script cai no acumulado do dia atual e avisa isso na
própria linha, para ninguém ler um número pela metade como se fosse o dia todo.

## Testar na sua máquina, sem credencial

```bash
# terminal 1
python mock_server.py

# terminal 2
export AUXSOL_BASE_URL=http://127.0.0.1:8899/prod-api
export AUXSOL_APP_ID=1 AUXSOL_APP_SECRET=2
export GCHAT_WEBHOOK_URL=http://127.0.0.1:8899/gchat
python main.py relatorio
```

O `mock_server.py` devolve dados sintéticos com os três estados de propósito —
Tijucas gerando bem, Bairro Novo fraco, Makiolka parada e com alarme — e imprime
no terminal o card que teria ido para o Chat. Use `DEBUG=1` para ver cada
chamada de API e `DRY_RUN=1` para imprimir o card em vez de enviar.

## Problemas comuns

| Sintoma no log | Causa e correção |
|---|---|
| `can't open file 'main.py'` | o `main.py` não está na raiz do repositório |
| `Secrets ausentes: ...` | falta cadastrar em Settings → Secrets and variables |
| `falha na autenticação — HTTP 404` | `AUXSOL_BASE_URL` errada; rode `descobrir-url` |
| `falha na autenticação — HTTP 401/403` | app_id/app_secret errados, ou credencial não liberada |
| `a API não retornou nenhuma usina` | a credencial autentica mas não vê os postos: confirmar a conta com a Nansen |
| `Google Chat recusou o card — HTTP 404` | webhook apagado ou URL truncada (ela tem `key=` **e** `token=`) |
| `HTTP 429` | limite de 10 req/5 s; o script já espaça as chamadas, mas duas execuções simultâneas estouram |
| card chega sem o texto da IA | sem `GEMINI_API_KEY`, ou a chave expirou — os números continuam corretos |
| relatório atrasa 10–20 min | normal: o agendador do GitHub atrasa em horários cheios. Para adiantar, troque o cron para `45 9 * * *` |

## Notas técnicas

- **Fuso.** O cron do GitHub é sempre UTC. `0 10 * * *` = 7h em Brasília, e o
  Brasil não tem mais horário de verão, então não muda ao longo do ano.
- **Limite de requisições.** A AUXSOL permite 10 requisições por 5 segundos por
  IP. O `main.py` usa uma janela deslizante com margem (9) e o workflow tem
  `concurrency` para não rodar duas vezes ao mesmo tempo.
- **Token.** Vale 12 h. Uma execução usa um só; se a API recusar no meio, o
  cliente reautentica uma vez e repete a chamada.
- **Leitura tolerante dos campos.** O formato exato da resposta da Nansen ainda
  não foi visto com credencial real. O script procura cada dado por uma lista de
  nomes possíveis (`dayEnergy`, `todayEnergy`, `eDay`, …) em vez de fixar um só,
  e o que não achar aparece como `—` no card, com aviso — em vez de derrubar o
  relatório. Se algum número vier errado ou vazio, rode com `DEBUG=1` e o log
  mostra o JSON cru: com ele, é ajustar a lista de nomes correspondente.
- **Cálculos.** Eficiência, kWh/kWp, semáforo e totais são calculados em Python.
  A IA só escreve o comentário e nunca é a fonte de um número — foi decisão de
  projeto, para o relatório não errar conta.
