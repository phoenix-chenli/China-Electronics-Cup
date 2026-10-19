import test from 'node:test';
import assert from 'node:assert/strict';
import { exactCitationSpan, findTextMatches, readerCsv, readerCellText, resolveReaderPage } from '../src/readerTools.ts';

test('explicit URL page takes priority over a saved reader page and citation', () => {
  assert.equal(resolveReaderPage([{number:1},{number:3},{number:5}], 5, 3, 1), 5);
});
test('reader page restoration falls back only to actual source pages', () => {
  const pages=[{number:1},{number:3},{number:5}];
  assert.equal(resolveReaderPage(pages, undefined, 3, 1),3);
  assert.equal(resolveReaderPage(pages, 4, 3, 1),3);
  assert.equal(resolveReaderPage(pages, undefined, 4, 5),5);
  assert.equal(resolveReaderPage(pages, 0, 4, 9),1);
  assert.equal(resolveReaderPage([],5,3,1),null);
});

test('citation offsets stay in Unicode code points including astral characters', () => {
  const page = { number: 3, text: '甲😀收入为10元。' };
  assert.deepEqual(exactCitationSpan(page, { page: 3, quote: '收入', start: 2, end: 4 }), [2,4]);
  assert.equal(exactCitationSpan(page, { page: 3, quote: '收入', start: 3, end: 5 }), null);
});
test('explicit wrong offsets never fall back to a coincidental matching quote', () => {
  const page={ number:1,text:'收入为10元' };
  assert.equal(exactCitationSpan(page,{page:1,quote:'收入',start:1,end:3}),null);
  assert.equal(exactCitationSpan(page,{page:2,quote:'收入'}),null);
});
test('quote fallback requires exactly one occurrence and preserves code point location', () => {
  assert.deepEqual(exactCitationSpan({number:1,text:'😀收入'},{page:1,quote:'收入'}),[1,3]);
  assert.equal(exactCitationSpan({number:1,text:'收入 收入'},{page:1,quote:'收入'}),null);
});
test('find searches literal text across real pages, not regex syntax', () => {
  const pages=[{number:2,text:'公式 [a+b]，金额 (1.2)'},{number:5,text:'复核 [a+b]'}];
  assert.deepEqual(findTextMatches(pages,'[a+b]').map(m=>m.page),[2,5]);
  assert.equal(findTextMatches(pages,'.*').length,0);
  assert.equal(findTextMatches(pages,'   ').length,0);
});
test('search offsets are Unicode safe and case insensitive', () => {
  assert.deepEqual(findTextMatches([{number:9,text:'😀Invoice INVOICE'}],'invoice'),[
    {page:9,start:1,end:8},{page:9,start:9,end:16},
  ]);
});
test('CSV escapes quotes and line breaks while preserving genuine negative numbers', () => {
  const csv=readerCsv(['名称','数值'],[['a,"b"\nc',-7],[null,0]]);
  assert.equal(csv,'\uFEFF"名称","数值"\r\n"a,""b""\nc","-7"\r\n"","0"');
});
test('CSV neutralizes formula strings in headers and cells including leading whitespace', () => {
  const csv=readerCsv(['=HYPERLINK("x")'],[[' =SUM(1,2)'],['+cmd'],['-cmd'],['@cmd'],['\t=cmd']]);
  assert.match(csv,/"'=HYPERLINK/);
  assert.match(csv,/"' =SUM/);
  assert.match(csv,/"'\+cmd"/);
  assert.match(csv,/"'-cmd"/);
  assert.match(csv,/"'@cmd"/);
  assert.match(csv,/"'\t=cmd"/);
});
test('NULL stays distinguishable from 0 and ordinary text in the reader', () => {
  assert.equal(readerCellText(null),'NULL');
  assert.equal(readerCellText(0),'0');
  assert.equal(readerCellText(''),'');
  assert.equal(readerCellText({a:1}),'{"a":1}');
});
