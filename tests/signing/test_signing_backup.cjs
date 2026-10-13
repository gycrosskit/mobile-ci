const test = require('node:test'), assert = require('node:assert/strict');
const crypto = require('node:crypto'), fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const {spawnSync} = require('node:child_process');
const backup = require('../../signing/signing_backup.cjs');
const pairs = Array.from({length:2}, () => crypto.generateKeyPairSync('rsa', {modulusLength:3072,
  publicKeyEncoding:{type:'spki', format:'pem'}, privateKeyEncoding:{type:'pkcs8', format:'pem'}}));
test('backup roundtrip authenticates ciphertext, key and format', () => {
  const key = crypto.randomBytes(32), bytes = crypto.randomBytes(512), sealed = backup.seal(bytes,key);
  assert.deepEqual(backup.open(sealed,key),bytes);
  assert.notEqual(backup.seal(bytes,key).nonce,sealed.nonce);
  assert.throws(()=>backup.open(sealed,crypto.randomBytes(32)));
  for (const field of ['ciphertext','tag','nonce']) {
    const changed = {...sealed}, value = Buffer.from(changed[field],'base64'); value[0] ^= 1; changed[field] = value.toString('base64');
    assert.throws(()=>backup.open(changed,key));
  }
  assert.throws(()=>backup.open({...sealed,format:'other'},key));
});
test('recovery key requires private owned regular file and rejects symlinks', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(),'signing-backup-test-')), file = path.join(dir,'key');
  try {
    const key=crypto.randomBytes(32);fs.writeFileSync(file,key,{mode:0o600});assert.deepEqual(backup.privateKey(file),key);
    fs.chmodSync(file,0o644);assert.throws(()=>backup.privateKey(file));fs.chmodSync(file,0o600);
    const link=path.join(dir,'link');fs.symlinkSync(file,link);assert.throws(()=>backup.privateKey(link));
  } finally { fs.rmSync(dir,{recursive:true,force:true}); }
});
test('v2 authenticates recipient and envelope using RSA-3072 OAEP SHA-256', () => {
  const bytes = crypto.randomBytes(512), pair = pairs[0], sealed = backup.sealPublic(bytes, pair.publicKey);
  assert.equal(sealed.format, 'sxmqlive-signing-backup-v2');
  assert.equal(sealed.keyAlgorithm, 'RSA-OAEP-SHA256');
  assert.equal(sealed.recipientSha256, crypto.createHash('sha256').update(crypto.createPublicKey(pair.publicKey).export({type:'spki',format:'der'})).digest('hex'));
  assert.equal(Buffer.from(sealed.wrappedKey, 'base64').length, 384);
  assert.deepEqual(backup.openPublic(sealed, pair.privateKey), bytes);
  const second = backup.sealPublic(bytes, pair.publicKey);
  assert.notEqual(second.nonce, sealed.nonce);
  assert.notEqual(second.wrappedKey, sealed.wrappedKey);
  assert.throws(() => backup.openPublic(sealed, pairs[1].privateKey));
  assert.throws(() => backup.openPublic(sealed, pair.publicKey));
  for (const field of ['ciphertext','tag','nonce','wrappedKey']) {
    const value = Buffer.from(sealed[field], 'base64'); value[0] ^= 1;
    assert.throws(() => backup.openPublic({...sealed, [field]:value.toString('base64')}, pair.privateKey), field);
  }
  for (const changed of [{format:'other'}, {keyAlgorithm:'RSA-OAEP-SHA1'}, {recipientSha256:'0'.repeat(64)}, {recipientSha256:sealed.recipientSha256.toUpperCase()}]) {
    assert.throws(() => backup.openPublic({...sealed, ...changed}, pair.privateKey));
  }
  // 即使改成另一把真实接收公钥的摘要，包裹的内容密钥仍无法被该私钥解开。
  const other = backup.sealPublic(bytes, pairs[1].publicKey);
  assert.throws(() => backup.openPublic({...sealed, recipientSha256:other.recipientSha256}, pairs[1].privateKey));
});
test('v2 rejects invalid key specifications, encoding, lengths and empty bytes', () => {
  const pair = pairs[0], sealed = backup.sealPublic(Buffer.from('test'), pair.publicKey);
  const weak = crypto.generateKeyPairSync('rsa', {modulusLength:2048});
  const ec = crypto.generateKeyPairSync('ec', {namedCurve:'prime256v1'});
  const wrongExponent = crypto.generateKeyPairSync('rsa', {modulusLength:3072, publicExponent:3});
  for (const invalid of [weak, ec, wrongExponent]) {
    assert.throws(() => backup.sealPublic(Buffer.from('test'), invalid.publicKey.export({type:'spki',format:'pem'})));
    assert.throws(() => backup.openPublic(sealed, invalid.privateKey.export({type:'pkcs8',format:'pem'})));
  }
  assert.throws(() => backup.sealPublic(Buffer.alloc(0), pair.publicKey));
  assert.throws(() => backup.sealPublic(Buffer.alloc(64 * 1024 * 1024 + 1), pair.publicKey));
  for (const field of ['ciphertext','nonce','tag','wrappedKey']) {
    for (const invalid of ['', null, 1, '!!!', sealed[field] + '\n']) {
      assert.throws(() => backup.openPublic({...sealed, [field]:invalid}, pair.privateKey));
    }
  }
  for (const [field, length] of [['nonce',11], ['tag',15], ['wrappedKey',383]]) {
    assert.throws(() => backup.openPublic({...sealed, [field]:Buffer.alloc(length).toString('base64')}, pair.privateKey));
  }
  const wrappedShortKey = crypto.publicEncrypt({key:pair.publicKey, oaepHash:'sha256', padding:crypto.constants.RSA_PKCS1_OAEP_PADDING}, crypto.randomBytes(16));
  assert.throws(() => backup.openPublic({...sealed, wrappedKey:wrappedShortKey.toString('base64')}, pair.privateKey));
});
test('CLI creates encrypted private recipient once, encrypts with public key and recovers privately', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'signing-backup-cli-'));
  const helper = path.join(__dirname, '../../signing/signing_backup.cjs');
  const run = (args, input) => spawnSync(process.execPath, [helper, ...args], {input, encoding:'utf8'});
  const key = path.join(dir,'key'), publicFile = path.join(dir,'recipient.pem'), privateFile = path.join(dir,'recipient.enc.json');
  const encrypted = path.join(dir,'archive.enc.json'), output = path.join(dir,'archive.tar'), bytes = Buffer.from('private archive bytes');
  try {
    fs.chmodSync(dir, 0o700);
    assert.equal(run(['keygen', key]).status, 0);
    assert.equal(run(['recipient-key', key, publicFile, privateFile]).status, 0);
    assert.equal(fs.statSync(privateFile).mode & 0o777, 0o600);
    const publicPem = fs.readFileSync(publicFile), privateEncrypted = fs.readFileSync(privateFile);
    assert.match(publicPem.toString(), /^-----BEGIN PUBLIC KEY-----/);
    assert.equal(JSON.parse(privateEncrypted).format, 'sxmqlive-signing-backup-v1');
    assert.equal(privateEncrypted.includes('PRIVATE KEY'), false);
    const privatePem = backup.open(JSON.parse(privateEncrypted), backup.privateKey(key));
    assert.match(privatePem.toString(), /^-----BEGIN PRIVATE KEY-----/);
    assert.notEqual(run(['recipient-key', key, publicFile, privateFile]).status, 0);
    assert.deepEqual(fs.readFileSync(publicFile), publicPem);
    assert.deepEqual(fs.readFileSync(privateFile), privateEncrypted);
    const anotherPublic = path.join(dir, 'another.pem');
    assert.notEqual(run(['recipient-key', key, anotherPublic, privateFile]).status, 0);
    assert.equal(fs.existsSync(anotherPublic), false);
    const encryption = run(['encrypt-public', publicFile, encrypted], bytes);
    assert.equal(encryption.status, 0);
    assert.equal(JSON.parse(encryption.stdout).verified, false);
    assert.equal(fs.statSync(encrypted).mode & 0o777, 0o600);
    assert.notEqual(run(['encrypt-public', publicFile, encrypted], bytes).status, 0);
    assert.notEqual(run(['verify', key, encrypted]).status, 0);
    assert.equal(run(['verify-public', key, privateFile, encrypted]).status, 0);
    assert.equal(run(['decrypt-public', key, privateFile, encrypted, output]).status, 0);
    assert.deepEqual(fs.readFileSync(output), bytes);
    assert.equal(fs.statSync(output).mode & 0o777, 0o600);
    assert.notEqual(run(['decrypt-public', key, privateFile, encrypted, output]).status, 0);
    assert.deepEqual(fs.readFileSync(output), bytes);
    assert.notEqual(run(['verify-public', publicFile, privateFile, encrypted]).status, 0);
    const wrongKey = path.join(dir,'wrong-key');
    assert.equal(run(['keygen', wrongKey]).status, 0);
    assert.notEqual(run(['verify-public', wrongKey, privateFile, encrypted]).status, 0);
    const legacy = path.join(dir, 'legacy.enc.json'), legacyOutput = path.join(dir, 'legacy.tar');
    assert.equal(run(['encrypt', key, legacy], bytes).status, 0);
    assert.equal(run(['verify', key, legacy]).status, 0);
    assert.equal(run(['decrypt', key, legacy, legacyOutput]).status, 0);
    assert.deepEqual(fs.readFileSync(legacyOutput), bytes);
    const linkedPublic = path.join(dir,'link.pem'); fs.symlinkSync(publicFile, linkedPublic);
    assert.notEqual(run(['encrypt-public', linkedPublic, path.join(dir,'link.enc')], bytes).status, 0);
    const publicDir = path.join(dir, 'public'); fs.mkdirSync(publicDir, {mode:0o755}); fs.chmodSync(publicDir, 0o755);
    assert.notEqual(run(['recipient-key', key, path.join(dir,'unused.pem'), path.join(publicDir,'private.enc')]).status, 0);
    assert.notEqual(run(['decrypt-public', key, privateFile, encrypted, path.join(publicDir,'archive')]).status, 0);
    assert.deepEqual(fs.readdirSync(dir).sort(), ['archive.enc.json','archive.tar','key','legacy.enc.json','legacy.tar','link.pem','public','recipient.enc.json','recipient.pem','wrong-key'].sort());
  } finally { fs.rmSync(dir, {recursive:true,force:true}); }
});
