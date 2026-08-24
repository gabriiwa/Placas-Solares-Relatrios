/**
 * teste_local.js — roda o Historico.gs fora do Google, com node.
 *
 *   node apps_script/teste_local.js
 *
 * POR QUE EXISTE
 * O Apps Script só roda no Google, e a única forma de testá-lo lá é implantar e
 * olhar a planilha. Este arquivo finge o SpreadsheetApp com uma matriz em
 * memória — inclusive a parte que causou o bug mais caro deste código: o Sheets
 * CONVERTE a string "2026-08-24" em data quando a coluna não está formatada
 * como texto, e a partir daí a chave nunca mais casa e cada noite duplica a
 * linha.
 *
 * Não substitui a função `testar()` dentro do editor (que prova permissão e
 * implantação), mas pega erro de lógica em segundos em vez de em dias.
 */

const fs = require('fs');
const path = require('path');

// --------------------------------------------------------------------------
// planilha falsa
// --------------------------------------------------------------------------

class FakeSheet {
  constructor(nome) {
    this.nome = nome;
    this.celulas = new Map();   // "linha,coluna" -> valor
    this.formatos = new Map();  // "linha,coluna" -> formato
    this.maxRows = 1000;
    this.congeladas = 0;
  }

  getName() { return this.nome; }
  getMaxRows() { return this.maxRows; }
  setFrozenRows(n) { this.congeladas = n; }

  _chave(l, c) { return l + ',' + c; }

  getLastRow() {
    let ultima = 0;
    for (const k of this.celulas.keys()) {
      const [l] = k.split(',').map(Number);
      if (this.celulas.get(k) !== '' && l > ultima) ultima = l;
    }
    return ultima;
  }

  getLastColumn() {
    let ultima = 0;
    for (const k of this.celulas.keys()) {
      const [, c] = k.split(',').map(Number);
      if (this.celulas.get(k) !== '' && c > ultima) ultima = c;
    }
    return ultima;
  }

  getRange(linha, coluna, nLinhas = 1, nColunas = 1) {
    const sheet = this;
    return {
      getValues() {
        const saida = [];
        for (let l = 0; l < nLinhas; l++) {
          const fila = [];
          for (let c = 0; c < nColunas; c++) {
            const v = sheet.celulas.get(sheet._chave(linha + l, coluna + c));
            fila.push(v === undefined ? '' : v);
          }
          saida.push(fila);
        }
        return saida;
      },
      getDisplayValues() {
        return this.getValues().map(function (fila) {
          return fila.map(function (v) {
            if (v instanceof Date) {
              // o Sheets mostraria no locale da planilha; pt-BR = dd/mm/aaaa
              const d = String(v.getDate()).padStart(2, '0');
              const m = String(v.getMonth() + 1).padStart(2, '0');
              return `${d}/${m}/${v.getFullYear()}`;
            }
            return String(v);
          });
        });
      },
      getDisplayValue() { return this.getDisplayValues()[0][0]; },
      setValues(valores) {
        for (let l = 0; l < valores.length; l++) {
          for (let c = 0; c < valores[l].length; c++) {
            const alvo = sheet._chave(linha + l, coluna + c);
            let v = valores[l][c];
            // AQUI está o comportamento que causou o bug: sem formato de
            // texto, o Sheets interpreta a string e guarda uma data.
            const formato = sheet.formatos.get(alvo);
            if (formato !== '@' && typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v)) {
              const [a, m, d] = v.split('-').map(Number);
              v = new Date(a, m - 1, d);
            }
            sheet.celulas.set(alvo, v);
          }
        }
        return this;
      },
      setNumberFormat(f) {
        for (let l = 0; l < nLinhas; l++) {
          for (let c = 0; c < nColunas; c++) {
            sheet.formatos.set(sheet._chave(linha + l, coluna + c), f);
          }
        }
        return this;
      },
      setFontWeight() { return this; },
    };
  }
}

class FakeSpreadsheet {
  constructor() { this.abas = new Map(); }
  getSheetByName(n) { return this.abas.get(n) || null; }
  insertSheet(n) { const s = new FakeSheet(n); this.abas.set(n, s); return s; }
  deleteSheet(s) { this.abas.delete(s.getName()); }
}

let planilhaAtual = new FakeSpreadsheet();

global.SpreadsheetApp = { getActiveSpreadsheet: () => planilhaAtual };
global.LockService = {
  getScriptLock: () => ({ waitLock() {}, releaseLock() {} }),
};
global.ContentService = {
  MimeType: { JSON: 'application/json' },
  createTextOutput: (t) => ({ setMimeType: () => ({ corpo: t }) }),
};
global.Logger = { log: (...a) => console.log('   [log]', ...a) };

// --------------------------------------------------------------------------
// carrega o Historico.gs de verdade
// --------------------------------------------------------------------------

const fonte = fs.readFileSync(path.join(__dirname, 'Historico.gs'), 'utf8');
// eslint-disable-next-line no-eval
eval(fonte);

// --------------------------------------------------------------------------
// cenários
// --------------------------------------------------------------------------

let falhas = 0;

function checar(rotulo, condicao, detalhe) {
  if (condicao) {
    console.log('  ok    ' + rotulo);
  } else {
    console.log('  FALHA ' + rotulo + (detalhe ? ' — ' + detalhe : ''));
    falhas++;
  }
}

function novaPlanilha() { planilhaAtual = new FakeSpreadsheet(); }

const CHAVE = ['data', 'plant_id'];
const linha = (data, id, kwh) => ({
  data, plant_id: id, usina: 'Usina ' + id, kwh_dia: kwh, parcial: 0,
});

console.log('\n1. aba vazia: insere e a data volta como TEXTO');
novaPlanilha();
let r = _gravar('historico', [linha('2026-08-24', '1348', 341.2), linha('2026-08-24', '1408', 156)], CHAVE);
checar('2 inseridas', r.inseridas === 2 && r.atualizadas === 0, JSON.stringify(r));
let mostrado = planilhaAtual.getSheetByName('historico').getRange(2, 1).getDisplayValue();
checar('a data continua "2026-08-24"', mostrado === '2026-08-24', 'veio ' + mostrado);

console.log('\n2. regravação do mesmo dia: ATUALIZA, não duplica (o bug caro)');
r = _gravar('historico', [linha('2026-08-24', '1348', 355)], CHAVE);
checar('0 inseridas, 1 atualizada', r.inseridas === 0 && r.atualizadas === 1, JSON.stringify(r));
checar('total continua 2', r.total === 2, 'total ' + r.total);
const valorAtualizado = planilhaAtual.getSheetByName('historico').getRange(2, 4).getDisplayValue();
checar('o valor novo sobrescreveu o antigo', valorAtualizado === '355', 'veio ' + valorAtualizado);

console.log('\n3. dia seguinte: anexa');
r = _gravar('historico', [linha('2026-08-25', '1348', 300), linha('2026-08-25', '1408', 400)], CHAVE);
checar('2 inseridas, total 4', r.inseridas === 2 && r.total === 4, JSON.stringify(r));

console.log('\n4. chave repetida DENTRO do mesmo pedido: uma linha só');
r = _gravar('historico', [linha('2026-08-26', '1348', 1), linha('2026-08-26', '1348', 2)], CHAVE);
checar('total 5, não 6', r.total === 5, 'total ' + r.total);

console.log('\n5. coluna nova aparece depois: anexada, nada movido');
r = _gravar('historico', [
  Object.assign(linha('2026-08-27', '1348', 10), { injetado_kwh: 7.5 }),
], CHAVE);
const aba = planilhaAtual.getSheetByName('historico');
const cab = aba.getRange(1, 1, 1, aba.getLastColumn()).getDisplayValues()[0];
checar('cabeçalho começa com data,plant_id', cab[0] === 'data' && cab[1] === 'plant_id', cab.join('|'));
checar('injetado_kwh entrou no fim', cab[cab.length - 1] === 'injetado_kwh', cab.join('|'));
r = _gravar('historico', [linha('2026-08-27', '1348', 11)], CHAVE);
checar('a linha com coluna nova ainda é encontrada pela chave', r.atualizadas === 1, JSON.stringify(r));

console.log('\n6. dimensão com chave de uma coluna só');
novaPlanilha();
r = _gravar('usinas', [
  { plant_id: '1348', usina: 'Bairro Novo', kwp: 75 },
  { plant_id: '1408', usina: 'Tijucas', kwp: 100 },
], ['plant_id']);
checar('2 inseridas', r.inseridas === 2, JSON.stringify(r));
r = _gravar('usinas', [{ plant_id: '1348', usina: 'Bairro Novo', kwp: 80 }], ['plant_id']);
checar('atualiza sem duplicar', r.atualizadas === 1 && r.total === 2, JSON.stringify(r));

console.log('\n7. cabeçalho com coluna vazia NO MEIO: tem de parar com erro');
novaPlanilha();
const suja = planilhaAtual.insertSheet('historico');
suja.getRange(1, 1, 1, 4).setNumberFormat('@').setValues([['data', '', 'plant_id', 'usina']]);
suja.getRange(2, 1, 1, 4).setNumberFormat('@').setValues([['2026-08-24', 'x', '1348', 'BN']]);
let barrou = false;
try {
  _gravar('historico', [linha('2026-08-25', '1348', 1)], CHAVE);
} catch (e) {
  barrou = /sem nome/.test(String(e));
}
checar('recusou gravar em cabeçalho desalinhado', barrou, 'gravou mesmo assim');

console.log('\n8. cauda vazia no cabeçalho: aceita normalmente');
novaPlanilha();
const comCauda = planilhaAtual.insertSheet('historico');
comCauda.getRange(1, 1, 1, 2).setNumberFormat('@').setValues([['data', 'plant_id']]);
let ok8 = true;
try {
  r = _gravar('historico', [linha('2026-08-24', '1348', 5)], CHAVE);
} catch (e) { ok8 = false; console.log('   erro:', String(e)); }
checar('gravou', ok8 && r.inseridas === 1, JSON.stringify(r));

console.log('\n9. doPost: token errado é recusado, token certo grava');
novaPlanilha();
const recusa = JSON.parse(doPost({ postData: { contents: JSON.stringify({
  token: 'errado', aba: 'historico', chave: CHAVE, linhas: [linha('2026-08-24', '1348', 1)],
}) } }).corpo);
checar('token errado recusado', recusa.ok === false, JSON.stringify(recusa));
TOKEN = 'segredo-de-teste';
const aceita = JSON.parse(doPost({ postData: { contents: JSON.stringify({
  token: 'segredo-de-teste', aba: 'historico', chave: CHAVE,
  linhas: [linha('2026-08-24', '1348', 1)],
}) } }).corpo);
checar('token certo grava 1 linha', aceita.ok === true && aceita.inseridas === 1, JSON.stringify(aceita));

console.log(falhas === 0 ? '\nTodos os cenários passaram.\n' : `\n${falhas} FALHA(S).\n`);
process.exit(falhas === 0 ? 0 : 1);
