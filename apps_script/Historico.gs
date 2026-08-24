/**
 * Historico.gs — recebe as linhas do relatório diário e grava numa planilha.
 *
 * PARA QUE SERVE
 * A API da Nansen não guarda série diária. Este script é o caderno onde o
 * histórico vai sendo anotado, um dia por vez. O Power BI depois lê a planilha
 * pelo conector nativo do Google Sheets — sem gateway, sem licença extra,
 * sem deixar nada público.
 *
 * POR QUE UM APPS SCRIPT E NÃO A API DO SHEETS
 * A API oficial exige assinar um JWT com RS256, o que em Python significa
 * `pip install cryptography`. O main.py é só biblioteca padrão de propósito
 * (nada para instalar, nada para quebrar numa atualização). Um Apps Script
 * publicado como app web reduz o lado Python a um POST de JSON — a mesma
 * coisa que o webhook do Google Chat, que já funciona.
 *
 * COMO INSTALAR (uma vez, ~5 minutos)
 *
 *  1. Crie uma planilha no Drive. Nome sugerido: "Usinas — histórico diário".
 *  2. Extensões > Apps Script. Apague o conteúdo e cole este arquivo.
 *  3. Troque o valor de TOKEN abaixo por um segredo qualquer, longo e aleatório
 *     (ex: o que sair de `openssl rand -hex 24`). Ele é a única coisa que
 *     impede um estranho de escrever na planilha, então não reaproveite senha.
 *  4. Salve (💾).
 *  5. Implantar > Nova implantação > tipo "App da Web".
 *       Executar como:        Eu
 *       Quem pode acessar:    Qualquer pessoa
 *     "Qualquer pessoa" assusta, mas é obrigatório: o GitHub Actions não faz
 *     login no Google. Quem não tem o TOKEN recebe 403 e não escreve nada, e
 *     ninguém consegue LER a planilha por aqui — este script só grava.
 *  6. Copie a URL da implantação (termina em /exec).
 *  7. No GitHub, em Settings > Secrets and variables > Actions, crie:
 *       HISTORICO_WEBHOOK_URL   = a URL /exec
 *       HISTORICO_WEBHOOK_TOKEN = o mesmo TOKEN daqui
 *
 * IMPORTANTE AO ATUALIZAR: toda vez que editar este script é preciso
 * Implantar > Gerenciar implantações > ✏️ > Nova versão. Sem isso a URL
 * continua servindo o código antigo — e a gravação "funciona" sem mudar nada,
 * que é o pior tipo de falha.
 *
 * REGRA DE GRAVAÇÃO: substitui pela chave, não empilha.
 * A chave vem no próprio pedido (data+plant_id no histórico, plant_id nas
 * usinas). Rodar o relatório duas vezes no mesmo dia atualiza a linha em vez
 * de criar uma segunda — senão qualquer soma no Power BI sairia dobrada.
 */

var TOKEN = '4c23a81af18e0d49ea9795fdb94b6f204ae9090ac028ec88';

function doPost(e) {
  var trava = LockService.getScriptLock();
  // Duas execuções ao mesmo tempo leriam a mesma última linha e uma sobrescreveria
  // a outra. 30 s é folga suficiente para 3 usinas.
  try {
    trava.waitLock(30000);
  } catch (err) {
    return _json({ ok: false, erro: 'outra gravação em andamento' });
  }

  try {
    var pedido = JSON.parse(e.postData.contents);

    if (!TOKEN || TOKEN === 'TROQUE-ISTO-POR-UM-SEGREDO-LONGO') {
      return _json({ ok: false, erro: 'TOKEN não configurado no script' });
    }
    if (pedido.token !== TOKEN) {
      return _json({ ok: false, erro: 'token invalido' });
    }

    var linhas = pedido.linhas || [];
    if (!linhas.length) {
      return _json({ ok: true, inseridas: 0, atualizadas: 0, nota: 'nada enviado' });
    }

    var res = _gravar(
      pedido.aba || 'historico',
      linhas,
      pedido.chave || ['data', 'plant_id']
    );
    res.ok = true;
    return _json(res);
  } catch (err) {
    return _json({ ok: false, erro: String(err) });
  } finally {
    trava.releaseLock();
  }
}

/** GET serve só para conferir no navegador que a implantação está de pé. */
function doGet() {
  return _json({ ok: true, servico: 'historico-usinas', metodo: 'use POST' });
}

function _gravar(nomeAba, linhas, chave) {
  var planilha = SpreadsheetApp.getActiveSpreadsheet();
  var aba = planilha.getSheetByName(nomeAba) || planilha.insertSheet(nomeAba);

  // Cabeçalho: as colunas vêm do primeiro pedido. Colunas novas que apareçam
  // depois são ANEXADAS no fim; nenhuma coluna existente é movida ou apagada,
  // porque o Power BI referencia coluna por nome e por posição.
  var colunas = _cabecalho(aba, linhas);

  var indiceChave = chave.map(function (c) { return colunas.indexOf(c); });
  if (indiceChave.some(function (i) { return i < 0; })) {
    throw new Error('a chave ' + chave.join('+') + ' não existe no cabeçalho da aba ' + nomeAba);
  }

  // FORMATAR AS COLUNAS-CHAVE COMO TEXTO **ANTES** DE QUALQUER ESCRITA.
  //
  // Este é o bug mais caro que este arquivo já teve. O setValues aplica a mesma
  // interpretação de quem digita na célula: numa coluna de formato automático,
  // a string "2026-08-24" é guardada como DATA, não como texto. Formatar depois
  // muda só a aparência. Na requisição seguinte o getValues devolve um objeto
  // Date, o String() dele sai "Sun Aug 24 2026 …", a chave não casa com
  // "2026-08-24" — e a linha é ANEXADA de novo. Resultado: cada regravação
  // duplica o dia, e a resposta diz "1 inserida", ou seja, sucesso.
  // No Power BI a soma sairia dobrada, sem nada indicando o motivo.
  //
  // Formatar a coluna inteira (getMaxRows) resolve para as linhas futuras.
  indiceChave.forEach(function (i) {
    aba.getRange(1, i + 1, aba.getMaxRows(), 1).setNumberFormat('@');
  });

  var totalLinhas = aba.getLastRow();
  // getDisplayValues, e não getValues: devolve o que está escrito na célula,
  // já como texto. Se uma linha antiga tiver sido gravada antes desta correção
  // (data virou Date), o display ainda é comparável depois de normalizado.
  var dados = totalLinhas > 1
    ? aba.getRange(2, 1, totalLinhas - 1, colunas.length).getDisplayValues()
    : [];

  var posicaoPorChave = {};
  for (var i = 0; i < dados.length; i++) {
    posicaoPorChave[_chaveDe(dados[i], indiceChave)] = i + 2; // +2: cabeçalho e base 1
  }

  var inseridas = 0;
  var atualizadas = 0;
  var novas = [];

  linhas.forEach(function (linha) {
    var valores = colunas.map(function (c) {
      var v = linha[c];
      return (v === undefined || v === null) ? '' : v;
    });
    var k = _chaveDe(valores, indiceChave);
    if (posicaoPorChave[k]) {
      var alvo = posicaoPorChave[k];
      if (alvo > 0) {
        aba.getRange(alvo, 1, 1, colunas.length).setValues([valores]);
        atualizadas++;
      } else {
        // Chave repetida DENTRO do mesmo pedido: sobrescreve a que já estava
        // na fila em vez de anexar duas. Acontece se a API listar a mesma
        // usina duas vezes (sobreposição de paginação, datalogger duplicado).
        novas[-alvo - 1] = valores;
      }
    } else {
      novas.push(valores);
      posicaoPorChave[k] = -novas.length; // negativo = índice dentro de `novas`
      inseridas++;
    }
  });

  if (novas.length) {
    aba.getRange(aba.getLastRow() + 1, 1, novas.length, colunas.length).setValues(novas);
  }

  aba.setFrozenRows(1);
  return { inseridas: inseridas, atualizadas: atualizadas, total: aba.getLastRow() - 1 };
}

function _cabecalho(aba, linhas) {
  var ultima = aba.getLastColumn();
  var existentes = ultima > 0 ? aba.getRange(1, 1, 1, ultima).getDisplayValues()[0] : [];
  existentes = existentes.map(function (c) { return String(c).trim(); });

  // Só a CAUDA vazia pode ser cortada. Um vazio no MEIO do cabeçalho não pode
  // ser filtrado: filtrar renumera as colunas enquanto os dados abaixo ficam
  // onde estão, e a partir daí toda chave é lida da coluna errada e todo
  // setValues escreve deslocado, sobrescrevendo histórico de verdade.
  // Parar com erro é a única saída segura — dá para consertar a planilha à mão;
  // dado sobrescrito não dá.
  while (existentes.length && existentes[existentes.length - 1] === '') {
    existentes.pop();
  }
  var vazioNoMeio = existentes.indexOf('');
  if (vazioNoMeio >= 0) {
    throw new Error(
      'a coluna ' + (vazioNoMeio + 1) + ' da aba "' + aba.getName() + '" está sem nome no ' +
      'cabeçalho. Nomeie ou apague a coluna antes de gravar — gravar assim ' +
      'desalinharia as colunas e sobrescreveria dados.'
    );
  }

  var vistas = {};
  existentes.forEach(function (c) { vistas[c] = true; });

  var extras = [];
  linhas.forEach(function (linha) {
    Object.keys(linha).forEach(function (c) {
      if (!vistas[c]) { vistas[c] = true; extras.push(c); }
    });
  });

  var colunas = existentes.concat(extras);
  if (extras.length || existentes.length === 0) {
    aba.getRange(1, 1, 1, colunas.length).setValues([colunas]);
    aba.getRange(1, 1, 1, colunas.length).setFontWeight('bold');
  }
  return colunas;
}

function _chaveDe(valores, indices) {
  return indices.map(function (i) { return String(valores[i]); }).join(' ');
}

function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

/**
 * Rode esta função uma vez no editor (▶ testar) para conferir que a planilha
 * aceita escrita e que o cabeçalho sai certo, sem precisar esperar o job das
 * 22h. Ela grava duas linhas na aba "teste" — apague a aba depois.
 */
function testar() {
  var planilha = SpreadsheetApp.getActiveSpreadsheet();
  var antiga = planilha.getSheetByName('teste');
  if (antiga) { planilha.deleteSheet(antiga); }  // começa limpo

  var chave = ['data', 'plant_id'];

  var r1 = _gravar('teste', [
    { data: '2026-08-24', plant_id: '1348', usina: 'Bairro Novo', kwh_dia: 341.2 },
    { data: '2026-08-24', plant_id: '1408', usina: 'Tijucas', kwh_dia: 156.0 }
  ], chave);
  Logger.log('1) primeira gravação: %s', JSON.stringify(r1));
  if (r1.inseridas !== 2 || r1.atualizadas !== 0) {
    throw new Error('a primeira gravação não inseriu as 2 linhas');
  }

  // O teste que importa: a data tem de voltar como TEXTO "2026-08-24". Se
  // voltar como data, a chave nunca casa e cada noite duplica a linha.
  var lida = planilha.getSheetByName('teste').getRange(2, 1).getDisplayValue();
  Logger.log('2) a data gravada volta como "%s"', lida);
  if (lida !== '2026-08-24') {
    throw new Error('a data virou ' + lida + ' — o Sheets converteu o texto em data');
  }

  var r2 = _gravar('teste', [
    { data: '2026-08-24', plant_id: '1348', usina: 'Bairro Novo', kwh_dia: 355.0 }
  ], chave);
  Logger.log('3) regravação (deve dar 0 inserida, 1 atualizada): %s', JSON.stringify(r2));
  if (r2.inseridas !== 0 || r2.atualizadas !== 1) {
    throw new Error('a substituição por chave não funcionou — conferir antes de usar');
  }

  // Mesma chave duas vezes no MESMO pedido: tem de virar uma linha só.
  var r3 = _gravar('teste', [
    { data: '2026-08-25', plant_id: '1348', usina: 'Bairro Novo', kwh_dia: 1 },
    { data: '2026-08-25', plant_id: '1348', usina: 'Bairro Novo', kwh_dia: 2 }
  ], chave);
  Logger.log('4) chave repetida no mesmo pedido: %s', JSON.stringify(r3));
  if (r3.total !== 3) {
    throw new Error('chave repetida no mesmo pedido criou linha duplicada (total=' + r3.total + ')');
  }

  Logger.log('ok — pode apagar a aba "teste"');
}
