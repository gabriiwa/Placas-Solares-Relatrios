# Dashboard no Power BI — o que já está pronto e o que falta

Escrito em 24/08/2026, na sessão em que a decisão foi tomada.

## As três decisões que abriram o caminho

1. **Power BI Pro já é pago pela empresa.** Isso muda a conta: não há
   assinatura nova para o CEO ver o relatório, e a alternativa gratuita
   ("Publicar na web", que deixa o conteúdo público para qualquer um com o
   link, sem login) fica descartada — o que é o certo para dado financeiro.
2. **O CEO quer explorar, não só olhar** — filtrar por usina, cruzar períodos.
   Isso justifica ferramenta de BI de verdade, e não uma página estática.
3. **O trabalho foi dividido:** o encanamento (coleta, gravação, correções) foi
   feito nesta sessão; o modelo e os visuais do Power BI são a parte do
   Gabriel — é onde o retorno é visual e imediato.

## O problema real, que não era o conector

O Power BI *consegue* chamar a API da Nansen direto. Não é por aí, por três
motivos:

- **A API não tem série diária.** Só totais por ano e o `todayYield`, que é um
  instantâneo. O Power BI substitui o dataset a cada atualização — ele não
  acumula. Uma conexão direta daria um dashboard capaz de mostrar apenas hoje,
  para sempre.
- O `app_secret` moraria dentro do `.pbix`, num arquivo que circula por e-mail.
- O limite de requisições (10/5 s no papel, 429 com 9 na prática) é gasto por
  cada atualização de cada pessoa.

Ou seja, faltava um **armazém**, não uma ferramenta. E o armazém tem pressa:
toda noite sem gravar é um dia de histórico que não volta.

## A arquitetura

```
   API Nansen  ──►  GitHub Actions (22h)  ──►  Google Chat (card diário)
                            │
                            ├──►  planilha do Google  ──►  Power BI (Pro)
                            │      (via Apps Script)         atualização diária
                            └──►  dados/historico.csv
                                   (2ª cópia, versionada no repositório)
```

Duas cópias de propósito: este é o único dado do projeto que não pode ser
recuperado se for perdido.

## O que foi feito nesta sessão

| Arquivo | O que é |
|---|---|
| `armazenamento.py` | grava uma linha por usina por dia. Substitui pela chave (data, plant_id) em vez de empilhar — rodar duas vezes no mesmo dia não dobra a geração |
| `apps_script/Historico.gs` | o Apps Script que recebe as linhas e escreve na planilha. Só stdlib do lado Python, sem `pip install` |
| `main.py` → `sondar-series` | responde se existe série diária e se o consumo/injeção vêm — as duas perguntas que mudam o esquema |
| `main.py` → `testar-historico` | confere a planilha sem esperar a noite e sem sujar dado real |
| `main.py` → `geracao_anual()` | **bug corrigido**: descarta o ano de entrada em operação. Era o que fazia o Tijucas anunciar "média de 40,1 kWh" com 100 kWp |
| `mock_server.py` | agora finge a planilha e imita a série anual com ano nulo — o caso do Tijucas dá para testar sem credencial |
| `apps_script/teste_local.js` | roda o Apps Script fora do Google (`node apps_script/teste_local.js`), com uma planilha falsa que imita até a conversão de texto em data |
| `_novo_workflow.yml` | o workflow novo, para colar no editor web (arquivos em `.github/workflows/` não podem ser escritos remotamente) |

Testado ponta a ponta contra o `mock_server.py`: gravação, regravação do mesmo
dia (substitui), destino fora do ar (o card sai, com aviso), `DRY_RUN` (não
grava), e o histórico do Tijucas saindo vazio em vez de errado.

### Cinco defeitos achados na revisão do próprio código, e corrigidos

Vale registrar porque três deles falhavam **em silêncio**, que é o modo de falha
que este projeto mais precisa evitar:

1. **O Sheets convertia `"2026-08-24"` em data ao gravar.** A formatação de
   texto estava sendo aplicada *depois* da escrita, o que muda só a aparência.
   Na noite seguinte a chave lida da planilha era `"Sun Aug 24 2026…"`, não
   casava, e a linha era **anexada de novo** — com resposta de sucesso. O Power
   BI somaria o dobro. Agora a coluna-chave é formatada como texto antes de
   qualquer escrita, e a leitura usa `getDisplayValues`.
2. **Perda total de histórico saía como relatório verde.** O card só mostrava
   aviso se a mensagem contivesse a palavra "falhou". Se o campo de id da API
   mudasse de nome, nenhuma linha teria chave, nada seria gravado, e a mensagem
   ("nenhuma linha com chave válida") não casava com o filtro. Agora o
   armazenamento devolve um `ok` explícito e qualquer coisa que não seja
   gravação confirmada vai para o card.
3. **`ok: true` com zero linhas passava por sucesso.** Um Apps Script implantado
   numa versão antiga responde ok sem gravar. Agora o número de linhas
   confirmadas é conferido contra o número enviado.
4. **O CSV era reescrito sem atomicidade** — um `timeout` no meio deixaria o
   histórico truncado. Agora grava em `.tmp` e faz `os.replace`.
5. **A rede de proteção do histórico anual só funcionava com dois anos ou
   mais** — justamente o caso do Tijucas (um ano só) escapava. Agora há também
   um teste de plausibilidade física (400 a 2.000 kWh/kWp/ano).

O `teste_local.js` reproduz o defeito nº 1 se a correção for removida — foi
assim que se conferiu que o teste não é decorativo.

### Cuidado ao testar localmente

Rodando contra o `mock_server.py`, aponte o CSV para um caminho de teste:

```
HISTORICO_CSV=dados/historico_teste.csv python main.py relatorio
```

`dados/*_teste.csv` está no `.gitignore`. Usar o caminho de produção grava
números sintéticos no arquivo que será comitado como histórico de verdade — e
um dia falso cuja data o job nunca revisita fica lá para sempre.

## Ativação — a parte que precisa das suas mãos

Na ordem. Os passos 1 a 4 podem ser feitos hoje; o Power BI só faz sentido
depois de algumas noites de dados.

**1. Criar a planilha e o Apps Script** (~5 min)
Siga o cabeçalho de `apps_script/Historico.gs`. No fim você tem uma URL `/exec`
e um TOKEN. Rode a função `testar()` no editor antes de sair: ela prova que a
substituição por chave funciona.

**2. Cadastrar dois secrets no GitHub**
`Settings > Secrets and variables > Actions`:
`HISTORICO_WEBHOOK_URL` (a URL /exec) e `HISTORICO_WEBHOOK_TOKEN` (o TOKEN).

**3. Trocar o workflow** (editor web do GitHub, `.github/workflows/daily_report.yml`)
Cole o conteúdo de `_novo_workflow.yml` e apague esse arquivo depois. Ele muda:

- **o cron de `0 10 * * *` para `0 1 * * *`** (22h de Brasília) — a pendência
  que está aberta desde 21/08. Enquanto não trocar, o card das 7h sai parcial
  todo dia **e o histórico grava um dia de geração quase zero**, que é pior:
  o dado errado fica.
- passa as variáveis do histórico e comita o CSV de volta no repositório;
- inclui `testar-historico` e `sondar-series` na lista de comandos manuais.

**4. Rodar os dois comandos manuais** (Actions > Run workflow)

- `testar-historico` → tem de aparecer uma linha na aba `teste` da planilha.
- `sondar-series` → **leia o veredito no fim do log.** Ele responde duas
  perguntas que mudam o que o dashboard consegue mostrar:
  - *existe série diária?* Se existir, dá para recarregar o histórico desde
    2024 em vez de começar de hoje. Olhe a coluna de faixa: se todas as
    combinações devolvem o mês corrente independentemente do `queryTime`
    pedido, não é série histórica — é a mesma resposta com outra roupa.
  - *o consumo e a injeção vêm?* As três usinas têm `meterFlag: 1`. Se vierem,
    a economia deixa de ser um teto estimado e passa a ser o número real — que
    é o que se compara com a parcela do financiamento, o objetivo original do
    projeto.

Me manda o log dessa execução antes de montar o modelo: o resultado pode
acrescentar colunas, e acrescentar antes é de graça.

**5. Esperar uma noite.** Depois da primeira execução das 22h a planilha tem a
primeira linha de verdade.

## O modelo no Power BI — sua parte

### Tabelas

| Tabela | De onde vem | Grão |
|---|---|---|
| `historico` | aba `historico` da planilha | um dia × uma usina |
| `usinas` | aba `usinas` da planilha | uma usina |
| `dCalendario` | criada em DAX (abaixo) | um dia |
| `financiamentos` | digitada à mão (Inserir dados) | uma usina |

`financiamentos` é manual porque esse dado não existe na API: `plant_id`,
`valor_financiado`, `valor_parcela`, `num_parcelas`, `primeira_parcela`.
Sem ela o dashboard funciona; com ela responde a pergunta que originou o
projeto.

### Conexão

Obter dados > **Google Sheets** > cole a URL da planilha > entre com a conta do
Google > selecione as abas `historico` e `usinas`. No Power Query:

- `data` → tipo **Data**. O texto vem em ISO (`2026-08-24`) exatamente para não
  depender de locale; se o Power BI insistir em interpretar como 24 de agosto ou
  08 de abril conforme a máquina, use **Usando Localidade > Inglês (EUA)**.
- `kwh_dia`, `kwh_esperado`, `economia_dia_rs`, … → **Número Decimal**.
- **Não** substitua vazio por zero. Vazio é "não foi lido"; zero é "usina
  parada". Trocar um pelo outro estraga qualquer média.

Publique num **Workspace**, não no "Meu workspace" — compartilhamento a partir
do workspace pessoal é limitado. Depois: `Configurações do semantic model >
Credenciais da fonte de dados` (entrar com a conta do Google) e
`Atualização agendada` para umas 23h BRT, depois do job. O conector do Google
Sheets é de nuvem: **não precisa de gateway**. Confirme na tela — se aparecer
"Conexão de gateway" obrigatória, algo está diferente do esperado e vale
verificar antes de seguir.

### Relacionamentos

```
dCalendario[Date]  1 ──► *  historico[data]
usinas[plant_id]   1 ──► *  historico[plant_id]
usinas[plant_id]   1 ──► 1  financiamentos[plant_id]
```

### dCalendario

```dax
dCalendario =
VAR PrimeiroDia = MIN(historico[data])
VAR UltimoDia   = MAX(historico[data])
RETURN
ADDCOLUMNS(
    CALENDAR(PrimeiroDia, UltimoDia),
    "Ano",     YEAR([Date]),
    "Mês nº",  MONTH([Date]),
    "Mês",     FORMAT([Date], "mmm"),
    "AnoMês",  FORMAT([Date], "yyyy-MM")
)
```

Marque como tabela de datas (`Ferramentas de tabela > Marcar como tabela de
datas`), senão as funções de tempo saem erradas em silêncio.

### Medidas

```dax
Geração (kWh) = SUM(historico[kwh_dia])

Esperado (kWh) = SUM(historico[kwh_esperado])

% do esperado = DIVIDE([Geração (kWh)], [Esperado (kWh)])

Capacidade (kWp) = SUM(usinas[kwp])

kWh/kWp = DIVIDE([Geração (kWh)], [Capacidade (kWp)])

Economia (R$) = SUM(historico[economia_dia_rs])

Dias com dado = DISTINCTCOUNT(historico[data])

Dias parciais = CALCULATE(DISTINCTCOUNT(historico[data]), historico[parcial] = 1)

Parcela mensal (R$) = SUM(financiamentos[valor_parcela])

Cobertura da parcela = DIVIDE([Economia (R$)], [Parcela mensal (R$)])

Economia acumulada (R$) =
CALCULATE(
    [Economia (R$)],
    FILTER(ALL(dCalendario), dCalendario[Date] <= MAX(dCalendario[Date]))
)
```

`Cobertura da parcela` acima de 1 significa que a economia do período pagou a
parcela. É o número que responde à pergunta do projeto — e o que provavelmente
vai para o topo da página.

### Três armadilhas que distorcem o painel em silêncio

1. **Dias parciais.** Uma linha com `parcial = 1` foi lida com o dia em curso e
   tem geração incompleta. Ela é útil no histórico mas envenena qualquer
   julgamento: filtre `parcial = 0` nos visuais de "% do esperado". A medida
   `Dias parciais` existe para você ver quantos entraram na conta.
   Repare que `Geração (kWh)` **não** filtra `parcial` — de propósito, porque um
   dia parcial ainda é geração que aconteceu. A consequência é que um total
   mensal com dias parciais dentro fica subestimado. Se isso incomodar, use
   `Dias parciais` ao lado do total, ou crie uma segunda medida filtrada; o que
   não dá é mostrar o total sem indicar que há dia incompleto nele.
2. **A economia é um teto.** `economia_dia_rs` assume todo kWh valendo tarifa
   cheia (R$ 0,87 cadastrados na plataforma). O excedente injetado não compensa
   100% da TUSD pela Lei 14.300/2022, então o valor real é menor. Escreva "teto
   estimado" no rótulo do visual até que (a) os R$ 0,87 sejam conferidos numa
   fatura e (b) a sondagem confirme as séries de consumo e injeção.
3. **O esperado é referência regional, não medição.** Vem da tabela
   `FORMA_MENSAL` (irradiação típica de Curitiba) × 0,80 de rendimento ×
   capacidade. É de propósito: comparar a usina consigo mesma premiava a que
   sempre gerou pouco. Mas é um número típico da região, não medido no telhado —
   uma referência otimista transforma operação normal em problema aparente.
   Calibrar depois de alguns meses.

### O que o dashboard vai dizer no primeiro dia

Com os números de 21/08 as três usinas estavam **abaixo** do esperado no mês:
Bairro Novo 81%, Makiolka 44%, Tijucas 26%. E o Tijucas fazia 1,56 kWh/kWp
contra 4,55 do Bairro Novo, no mesmo dia e na mesma cidade, tendo 100 kWp
contra 75 — devia ser o que gera mais e é o que gera menos.

Se isso se confirmar, é a manchete. Vale conferir a referência regional antes
de mostrar ao CEO, e vale mandar alguém olhar fisicamente o Tijucas antes da
reunião — chegar com o problema e a inspeção já pedida é uma conversa bem
diferente de chegar só com o gráfico vermelho.

## Pendências que sobraram

1. Conferir os R$ 0,87/kWh contra uma fatura real.
2. Rodar `alarmes` e confirmar se a credencial de convidado enxerga alarmes. Se
   vier sempre vazio, é restrição de permissão, não ausência de alarme — e um
   relatório que nunca acusa problema cria confiança falsa.
3. Preencher `financiamentos` no Power BI quando os dados do financiamento
   aparecerem.
4. Inspecionar fisicamente o Posto Tijucas.
5. Se a sondagem confirmar consumo/injeção: trocar a economia-teto pela
   economia real e acrescentar as colunas ao `armazenamento.py`.
