const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../../jenkins/controller/signing-portal');
const ui = require(path.join(root, 'app.js'));

test('only configured, enabled device signing brands can receive devices', () => {
  const enabled = {brand: 'a', platform: 'ios', configurationStatus: 'configured', channelEnabled: {pgyer: true}, team: 'one'};
  const rows = [enabled, {...enabled, brand: 'b', configurationStatus: 'pending'}, {...enabled, brand: 'c', channelEnabled: {testflight: true}}, {...enabled, brand: 'd', platform: 'harmony', channelEnabled: {designated: true}}];
  assert.deepEqual(ui.eligibleBrands(rows, 'ios'), [enabled]);
  assert.deepEqual(ui.eligibleBrands(rows, 'ios', {sources: [{brand: 'a', team: 'one'}]}), []);
  assert.deepEqual(ui.eligibleBrands(rows, 'ios', {sources: [{brand: 'a', team: 'other'}]}), [enabled]);
  assert.equal(ui.eligibleBrands(rows, 'harmony').length, 1);
});

test('sync all uses the complete platform pool and all eligible brands before apply', () => {
  const js = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
  const handler = `(() => {${js.match(/\$\("sync-all"\)\.addEventListener\("click", \(\) => \{([\s\S]*?)\n  \}\);/)[1]}})()`;
  const enabled = {brand:'a', displayName:'品牌 A', platform:'ios', team:'one', configurationStatus:'configured', channelEnabled:{pgyer:true}};
  const rows = [enabled, {...enabled, brand:'b', displayName:'品牌 B', team:'two'}, {...enabled, brand:'pending', configurationStatus:'pending'}, {...enabled, brand:'h', platform:'harmony', channelEnabled:{designated:true}}];
  const pools = {ios:[{id:'1',sources:[{brand:'a',team:'one'}]}, {id:'2',sources:[]}], harmony:[{id:'3'}]};
  for (const platform of ['ios','harmony']) {
    let body, summary, warning;
    const context = {...ui, state:{canMaintain:true,busy:false},active:false,uncertain:null, platform,
      rows:()=>rows, devices:()=>pools[platform], platformName:value=>value==='ios'?'iOS':'鸿蒙',
      preview:(value, text)=>{body=value;summary=text;},notice:text=>{warning=text;},
      $:()=>{throw new Error('bulk sync must not depend on search or visible device rows');}};
    const run = () => vm.runInNewContext(handler, context);
    run();
    assert.deepEqual(Array.from(body.deviceIds), pools[platform].map(device=>device.id));
    assert.deepEqual(Array.from(body.brands), platform==='ios'?['a','b']:['h']);
    assert.equal(body.platform,platform);assert.equal(body.source,'shared');assert.equal(body.operation,'devices');
    assert.equal(body.mode,undefined, 'use existing preview; never directly submit apply');
    assert.match(summary,/不受搜索筛选影响/);if(platform==='ios') assert.match(summary,/待接入/);
    for (const patch of [{state:{canMaintain:false}}, {state:{canMaintain:true,busy:true}}, {active:true}, {uncertain:{}}]) {
      body=null;vm.runInNewContext(handler,{...context,...patch});assert.equal(body,null);
    }
    body=null;context.devices=()=>[];run();assert.equal(body,null);
    context.devices=()=>Array.from({length:101},(_,i)=>({id:String(i)}));run();
    assert.equal(body,null);assert.match(warning,/100 台/);
    context.devices=()=>pools[platform];context.rows=()=>[];run();assert.equal(body,null);
  }
});

test('failed, cancelled and partial plans never unlock apply', () => {
  assert.equal(ui.canConfirm({state: 'finished', result: 'SUCCESS', brands: [{status: 'success'}]}), true);
  for (const task of [{state: 'running', result: 'SUCCESS'}, {state: 'cancelled', result: 'SUCCESS'}, {state: 'finished', result: 'FAILURE'}, {state: 'finished', result: 'UNSTABLE'}, {state: 'finished', result: 'SUCCESS', brands: [{status: 'unknown'}]}, {state: 'finished', result: 'SUCCESS', brands: [{status: 'not-run'}]}]) assert.equal(ui.canConfirm(task), false);
  assert.match(ui.taskSummary({state: 'finished', result: 'FAILURE'}), /失败/);
  assert.equal(ui.label('unknown'), '需核验');
});

test('public display escapes markup and only links to same-origin Jenkins', () => {
  assert.equal(ui.escape('<img src=x onerror="x">&'), '&lt;img src=x onerror=&quot;x&quot;&gt;&amp;');
  const base = 'https://ci.example/jenkins/signing/';
  assert.equal(ui.safeUrl('/jenkins/job/signing/1/', base), 'https://ci.example/jenkins/job/signing/1/');
  for (const value of ['javascript:alert(1)', 'https://evil.example/', 'https://name:password@ci.example/']) assert.equal(ui.safeUrl(value, base), null);
  assert.equal(ui.formatDate('bad'), '未知');
  assert.equal(ui.isStale('bad'), true);
  assert.equal(ui.isStale(new Date().toISOString()), false);
});

test('HTML contains every static JS element and has three entries', () => {
  const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
  const js = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size, ids.length);
  for (const [, id] of js.matchAll(/\$\("([^"]+)"\)/g)) assert.ok(ids.includes(id), `missing #${id}`);
  assert.deepEqual([...html.matchAll(/data-view="([^"]+)"/g)].map(match => match[1]), ['devices', 'add', 'signatures']);
  assert.doesNotMatch(js, /localStorage|sessionStorage|console\./);
  assert.match(js, /credentials: "same-origin"/);
  assert.match(js, /requestId: requestId\(\)/);
  new vm.Script(js);
});


test('request IDs work on HTTP where randomUUID is unavailable', () => {
  let calls = 0;
  const insecureCrypto = {getRandomValues(bytes) { calls++; bytes.fill(15); return bytes; }};
  assert.match(ui.requestId(insecureCrypto), /^[a-f0-9]{32}$/);
  assert.equal(calls, 1);
  assert.equal(ui.requestId({randomUUID: () => 'secure-uuid'}), 'secure-uuid');
  assert.equal(ui.teamLabel({companyName: '公司', teamId: 'TEAM123456', team: 'team_one'}), '公司 · TEAM123456 · team_one');
});


test('uncertain retries query the existing request and never create another operation', () => {
  const js = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
  const retry = js.match(/submit\(\{ \.\.\.body, recoverOnly: true \}, title, done\)/);
  assert.ok(retry, 'uncertain retry must set recoverOnly without regenerating requestId');
  const original = {requestId: 'the-original-request-id', mode: 'apply', brands: ['one']};
  let captured;
  vm.runInNewContext(retry[0], {body: original, title: 'retry', done() {}, submit(body) {captured = body;}});
  assert.equal(captured.requestId, original.requestId);
  assert.equal(captured.recoverOnly, true);
  assert.deepEqual(captured.brands, original.brands);
  assert.equal(original.recoverOnly, undefined);
  assert.match(js, /state\.skippedSnapshots/);
  assert.match(js, /detail\.code === "recovery-unavailable"/);
});


test('three steps validate device input before brand selection and plan submission', () => {
  const ios = 'A'.repeat(40), harmony = 'HM'.repeat(10);
  assert.equal(ui.formError(1, 'ios', '测试机', ios, []), '');
  assert.equal(ui.formError(1, 'ios', '测试机', '12345678-1234567890ABCDEF', []), '');
  assert.equal(ui.formError(1, 'harmony', '测试机', harmony, []), '');
  assert.match(ui.formError(1, 'ios', ' ', ios, []), /名称/);
  assert.match(ui.formError(1, 'ios', '测试机', 'not-a-udid', []), /UDID/);
  assert.match(ui.formError(1, 'harmony', '测试机', 'short', []), /UDID/);
  assert.match(ui.formError(1, 'ios', 'a'.repeat(81), ios, []), /名称过长/);
  for (const step of [2, 3]) {
    assert.match(ui.formError(step, 'ios', '测试机', ios, []), /选择.*品牌/);
    assert.equal(ui.formError(step, 'ios', '测试机', ios, ['one']), '');
    assert.match(ui.formError(step, 'ios', '测试机', '', ['one']), /UDID/);
  }
});

test('signature summaries require complete trustworthy evidence and never green unknown states', () => {
  const expiresAt = new Date(Date.now() + 60 * 86400000).toISOString();
  const row = {configurationStatus: 'configured', queryStatus: 'complete', materialStatus: 'configured', enabled: true, collectedAt: new Date().toISOString(), channelEnabled: {pgyer: true}, certificates: [{status: 'valid', expiresAt}], profiles: [{channel: 'pgyer', status: 'valid', expiresAt, identityVerified: true, materialReady: true}]};
  assert.deepEqual(ui.signatureSummary(row), {text: '台账检查通过', tone: 'good'});
  for (const patch of [{configurationStatus: 'unknown'}, {queryStatus: 'unknown'}, {queryStatus: 'partial'}, {errors: ['查询失败']}, {materialStatus: 'unknown'}, {materialStatus: 'missing'}, {enabled: false}, {channelEnabled: {}}, {certificates: []}, {profiles: []}, {collectedAt: 'bad'}, {collectedAt: new Date(Date.now() - 48 * 86400000).toISOString()}]) {
    assert.notEqual(ui.signatureSummary({...row, ...patch}).tone, 'good', JSON.stringify(patch));
  }
  assert.equal(ui.signatureSummary({...row, configurationStatus: 'pending'}).text, '待接入');
  assert.equal(ui.signatureSummary({...row, certificates: [{status: 'expired', expiresAt}]}).tone, 'bad');
  assert.equal(ui.signatureSummary({...row, certificates: [{status: 'valid', expiresAt: '2000-01-01'}]}).tone, 'bad');
  assert.equal(ui.signatureSummary({...row, certificates: [{status: 'expiring', expiresAt}]}).tone, 'warn');
  for (const patch of [{status: 'unknown'}, {status: 'mismatch'}, {expiresAt: 'bad'}, {identityVerified: false}, {materialReady: false}]) assert.notEqual(ui.signatureSummary({...row, profiles: [{...row.profiles[0], ...patch}]}).tone, 'good');
  assert.notEqual(ui.signatureSummary({...row, channelEnabled: {pgyer: true, testflight: true}}).tone, 'good');
});

test('device context uses OS signals rather than viewport width or Huawei brand', () => {
  const safari = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1';
  assert.deepEqual(ui.detectDevice(safari, 'iPhone', 5), {mobile:true,platform:'ios',safari:true});
  assert.equal(ui.detectDevice(safari + ' CriOS/130.0', 'iPhone', 5).safari, false);
  assert.deepEqual(ui.detectDevice('Mozilla/5.0 Macintosh Safari', 'MacIntel', 5), {mobile:true,platform:'ios',safari:true});
  assert.deepEqual(ui.detectDevice('Mozilla/5.0 Macintosh Safari', 'MacIntel', 0), {mobile:false,platform:null,safari:false});
  assert.deepEqual(ui.detectDevice('Mozilla/5.0 (Phone; OpenHarmony 6.0) Mobile HuaweiBrowser'), {mobile:true,platform:'harmony',safari:false});
  assert.deepEqual(ui.detectDevice('Mozilla/5.0 (Linux; Android 12; HUAWEI) Mobile Chrome'), {mobile:true,platform:null,safari:false});
  assert.equal(ui.detectDevice('unknown', '', 0, true).mobile, true);
  assert.equal(ui.detectDevice('unknown', '', 5, false).mobile, false);
});

test('signature overview uses real earliest expiry and keeps missing dates unknown', () => {
  assert.equal(ui.certificateExpiry({}), '待核验');
  assert.equal(ui.certificateExpiry({certificates:[{expiresAt:'bad'}]}), '待核验');
  assert.equal(ui.certificateExpiry({certificates:[{expiresAt:'2028-01-01'},{expiresAt:'2027-01-01'}]}), ui.formatDate('2027-01-01').split(' ')[0]);
});

test('Android signature evidence needs fingerprint agreement without a Profile or device whitelist', () => {
  const expiresAt = new Date(Date.now() + 60 * 86400000).toISOString();
  const row = {platform:'android', configurationStatus:'configured', queryStatus:'complete', materialStatus:'configured', enabled:true, collectedAt:new Date().toISOString(), channelEnabled:{}, certificates:[{status:'valid',expiresAt,matchesExpectedSha256:true}], profiles:[], devices:[]};
  assert.equal(ui.signatureSummary(row).tone,'good');
  assert.equal(ui.signatureSummary({...row,certificates:[{...row.certificates[0],matchesExpectedSha256:false}]}).tone,'bad');
  assert.notEqual(ui.signatureSummary({...row,certificates:[]}).tone,'good');
  assert.notEqual(ui.signatureSummary({...row,materialStatus:'invalid'}).tone,'good');
  assert.equal(ui.eligible({...row,channelEnabled:{designated:true}}),false);
  assert.deepEqual(ui.eligibleBrands([row], 'android'),[]);
});

test('Android appears only in signature view and switching to devices resets its platform', () => {
  const js = fs.readFileSync(path.join(root,'app.js'),'utf8');
  const render = js.match(/  function render\(\) \{[\s\S]*?\n  \}\n  function renderAcquisition/)[0].replace(/\n  function renderAcquisition$/,'');
  const nodes = new Map();
  const $ = id => {if(!nodes.has(id)) nodes.set(id,{value:'',hidden:false,prepend(){}}); return nodes.get(id);};
  const context = {platform:'android',view:'signatures',deviceContext:{platform:null,mobile:false},state:null,acquisition:'choose',addStep:1,syncDevice:null,
    document:{body:{dataset:{}},querySelectorAll:()=>[],querySelector:()=>({prepend(){}})},$, platformName:value=>value,formatDate:ui.formatDate,
    brandOptions(){},renderDevices(){},renderSignatures(){},renderAddStep(){}};
  vm.runInNewContext(render+'\nrender();',context);
  assert.equal(context.platform,'android');assert.equal($('android-platform').hidden,false);
  context.view='devices';vm.runInNewContext('render();',context);
  assert.equal(context.platform,'ios');assert.equal($('android-platform').hidden,true);
  context.platform='android';context.view='add';context.deviceContext.platform='harmony';vm.runInNewContext('render();',context);
  assert.equal(context.platform,'harmony');assert.equal($('android-platform').hidden,true);
  for (const [status, expected] of [['synced','已同步'],['unchanged','已同步'],['pending','待重试'],['running','进行中'],['none','暂无备份'],['unknown','暂无备份']]) {
    context.state={backup:{status,lastSuccess:{at:'2026-10-10T00:00:00Z'}}};
    vm.runInNewContext('render();',context);
    assert.equal($('backup-status').textContent,expected);
    assert.match($('backup-time').textContent,/最近同步/);
  }
  context.state=null;vm.runInNewContext('render();',context);
  assert.equal($('backup-status').textContent,'暂无备份');assert.equal($('backup-time').textContent,'');
  const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
  assert.match(html,/<label id="android-platform" hidden><input type="radio" name="platform" value="android">/);
  assert.doesNotMatch(html,/data-platform="android"/);
  assert.match(html,/href="#backup-status" target="_blank" rel="noopener noreferrer"/);
});

test('Android cards display public ownership, certificate, source and credential status', () => {
  const js = fs.readFileSync(path.join(root,'app.js'),'utf8');
  const code = js.match(/  function renderSignatures\(\) \{[\s\S]*?\n  function render\(\)/)[0].replace(/\n  function render\(\)$/,'');
  const row={brand:'a',displayName:'品牌 A',platform:'android',configurationStatus:'configured',queryStatus:'complete',materialStatus:'configured',enabled:true,collectedAt:new Date().toISOString(),keyOwner:'历史共享密钥组：one',keyAlias:'release',applicationId:'com.example.a',materialSource:'Jenkins',channelEnabled:{},
    certificates:[{name:'CN=<subject>',issuer:'CN=issuer',sha256:'a'.repeat(64),status:'valid',matchesExpectedSha256:true,expiresAt:'2030-01-01'}],profiles:[],credentials:[{role:'keystore',id:'android-key-one-keystore',status:'configured'}]};
  const list={innerHTML:''};
  vm.runInNewContext(code+'\nrenderSignatures();',{...ui,platform:'android',rows:()=>[row],badge:value=>ui.escape(ui.label(value)),$:id=>id==='search'?{value:''}:list});
  assert.match(list.innerHTML,/历史共享密钥组：one/);assert.match(list.innerHTML,/CN=&lt;subject&gt;/);assert.match(list.innerHTML,/签发者：CN=issuer/);
  assert.match(list.innerHTML,/SHA-256：a{64}/);assert.match(list.innerHTML,/android-key-one-keystore/);assert.match(list.innerHTML,/材料来源：Jenkins/);
  assert.match(list.innerHTML,/不使用 Profile 或设备白名单/);assert.doesNotMatch(list.innerHTML,/<small>测试 Profile/);
});
