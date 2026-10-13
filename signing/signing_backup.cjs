#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const FORMAT = 'sxmqlive-signing-backup-v1';
const PUBLIC_FORMAT = 'sxmqlive-signing-backup-v2';
const KEY_ALGORITHM = 'RSA-OAEP-SHA256';
const LIMIT = 64 * 1024 * 1024;

function privateKey(filename) {
  const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o600 || info.size !== 32) throw new Error('恢复密钥必须为本人持有的 0600、32 字节文件');
    return fs.readFileSync(fd);
  } finally { fs.closeSync(fd); }
}
function seal(bytes, key) {
  if (!bytes.length || bytes.length > LIMIT) throw new Error('备份为空或超过 64 MiB');
  const nonce = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', key, nonce);
  cipher.setAAD(Buffer.from(FORMAT));
  const ciphertext = Buffer.concat([cipher.update(bytes), cipher.final()]);
  return {format:FORMAT, nonce:nonce.toString('base64'), tag:cipher.getAuthTag().toString('base64'), ciphertext:ciphertext.toString('base64')};
}
function open(envelope, key) {
  if (envelope.format !== FORMAT || typeof envelope.ciphertext !== 'string' || envelope.ciphertext.length > Math.ceil(LIMIT * 4 / 3) + 4) throw new Error('备份格式或大小无效');
  const nonce = decodedField(envelope, 'nonce', 12), tag = decodedField(envelope, 'tag', 16);
  const ciphertext = decodedField(envelope, 'ciphertext');
  const decipher = crypto.createDecipheriv('aes-256-gcm', key, nonce);
  decipher.setAAD(Buffer.from(FORMAT)); decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
}
function rsaKey(pem, isPrivate = false) {
  const key = isPrivate ? crypto.createPrivateKey(pem) : crypto.createPublicKey(pem);
  if (key.asymmetricKeyType !== 'rsa' || key.asymmetricKeyDetails.modulusLength !== 3072 || key.asymmetricKeyDetails.publicExponent !== 65537n) throw new Error('接收密钥必须为 RSA-3072，指数 65537');
  return key;
}
function recipientSha256(publicKey) {
  return crypto.createHash('sha256').update(publicKey.export({type:'spki', format:'der'})).digest('hex');
}
function decodedField(envelope, field, length) {
  const value = envelope[field];
  if (typeof value !== 'string' || !value.length || value.length > Math.ceil((length || LIMIT) * 4 / 3) + 4) throw new Error('备份参数或大小无效');
  const bytes = Buffer.from(value, 'base64');
  if (bytes.toString('base64') !== value || (length ? bytes.length !== length : bytes.length > LIMIT)) throw new Error('备份编码或长度无效');
  return bytes;
}
function sealPublic(bytes, publicPem) {
  if (!bytes.length || bytes.length > LIMIT) throw new Error('备份为空或超过 64 MiB');
  const publicKey = rsaKey(publicPem), recipient = recipientSha256(publicKey);
  const key = crypto.randomBytes(32), nonce = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', key, nonce);
  cipher.setAAD(Buffer.from(PUBLIC_FORMAT + ':' + recipient));
  const ciphertext = Buffer.concat([cipher.update(bytes), cipher.final()]);
  // Node 的 oaepHash 同时约束 OAEP 与 MGF1；JCA 必须显式使用相同 SHA-256 参数。
  const wrappedKey = crypto.publicEncrypt({key:publicKey, padding:crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash:'sha256'}, key);
  return {format:PUBLIC_FORMAT, keyAlgorithm:KEY_ALGORITHM, recipientSha256:recipient,
    wrappedKey:wrappedKey.toString('base64'), nonce:nonce.toString('base64'), tag:cipher.getAuthTag().toString('base64'), ciphertext:ciphertext.toString('base64')};
}
function openPublic(envelope, privatePem) {
  if (!envelope || envelope.format !== PUBLIC_FORMAT || envelope.keyAlgorithm !== KEY_ALGORITHM || !/^[a-f0-9]{64}$/.test(envelope.recipientSha256)) throw new Error('备份格式或算法无效');
  const privateKey = rsaKey(privatePem, true);
  if (recipientSha256(crypto.createPublicKey(privateKey)) !== envelope.recipientSha256) throw new Error('备份接收密钥不匹配');
  const wrappedKey = decodedField(envelope, 'wrappedKey', 384), nonce = decodedField(envelope, 'nonce', 12);
  const tag = decodedField(envelope, 'tag', 16), ciphertext = decodedField(envelope, 'ciphertext');
  const key = crypto.privateDecrypt({key:privateKey, padding:crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash:'sha256'}, wrappedKey);
  if (key.length !== 32) throw new Error('备份内容密钥长度无效');
  const decipher = crypto.createDecipheriv('aes-256-gcm', key, nonce);
  decipher.setAAD(Buffer.from(PUBLIC_FORMAT + ':' + envelope.recipientSha256)); decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
}
function readFile(filename, limit = LIMIT * 2) {
  const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.size > limit) throw new Error('备份或密钥必须是有效普通文件');
    return fs.readFileSync(fd);
  } finally { fs.closeSync(fd); }
}
function privateDirectory(filename) {
  const info = fs.lstatSync(path.dirname(path.resolve(filename)));
  if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o700) throw new Error('密钥和解密输出的父目录必须为本人持有的 0700 目录');
}
async function main(args) {
  const [operation, keyFile, inputFile, outputFile] = args;
  if (operation === 'keygen' && args.length === 2) {
    privateDirectory(keyFile);
    fs.writeFileSync(keyFile, crypto.randomBytes(32), {mode:0o600, flag:'wx'});
    console.log(JSON.stringify({operation, created:true})); return;
  }
  if (operation === 'recipient-key' && args.length === 4) {
    privateDirectory(outputFile);
    const key = privateKey(keyFile);
    const pair = crypto.generateKeyPairSync('rsa', {modulusLength:3072, publicExponent:65537,
      publicKeyEncoding:{type:'spki', format:'pem'}, privateKeyEncoding:{type:'pkcs8', format:'pem'}});
    const encrypted = seal(Buffer.from(pair.privateKey), key);
    let publicFd, privateFd;
    try {
      publicFd = fs.openSync(inputFile, 'wx', 0o644);
      privateFd = fs.openSync(outputFile, 'wx', 0o600);
      fs.writeFileSync(publicFd, pair.publicKey);
      fs.writeFileSync(privateFd, JSON.stringify(encrypted) + '\n');
      const publicKey = rsaKey(readFile(inputFile, 16 * 1024));
      const recovered = open(JSON.parse(readFile(outputFile)), key);
      if (!recovered.equals(Buffer.from(pair.privateKey)) || recipientSha256(publicKey) !== recipientSha256(crypto.createPublicKey(rsaKey(recovered, true)))) throw new Error('接收密钥回读校验失败');
    } catch (error) {
      if (publicFd !== undefined) fs.unlinkSync(inputFile);
      if (privateFd !== undefined) fs.unlinkSync(outputFile);
      throw error;
    } finally {
      if (publicFd !== undefined) fs.closeSync(publicFd);
      if (privateFd !== undefined) fs.closeSync(privateFd);
    }
    console.log(JSON.stringify({operation, created:true, verified:true, recipientSha256:recipientSha256(rsaKey(pair.publicKey))})); return;
  }
  if (['encrypt','encrypt-public'].includes(operation) && args.length === 3) {
    const chunks = []; let size = 0;
    for await (const chunk of process.stdin) { size += chunk.length; if (size > LIMIT) throw new Error('备份超过 64 MiB'); chunks.push(chunk); }
    const bytes = Buffer.concat(chunks);
    let encrypted;
    if (operation === 'encrypt-public') encrypted = sealPublic(bytes, readFile(keyFile, 16 * 1024));
    else {
      const key = privateKey(keyFile);
      encrypted = seal(bytes, key);
      if (!open(encrypted, key).equals(bytes)) throw new Error('加密恢复校验失败');
    }
    fs.writeFileSync(inputFile, JSON.stringify(encrypted) + '\n', {mode:0o600, flag:'wx'});
    console.log(JSON.stringify({operation, verified:operation === 'encrypt', bytes:size, sha256:crypto.createHash('sha256').update(bytes).digest('hex')})); return;
  }
  if (['verify','decrypt'].includes(operation) && args.length === (operation === 'verify' ? 3 : 4)) {
    const bytes = open(JSON.parse(readFile(inputFile)), privateKey(keyFile));
    if (operation === 'decrypt') { privateDirectory(outputFile); fs.writeFileSync(outputFile, bytes, {mode:0o600, flag:'wx'}); }
    console.log(JSON.stringify({operation, verified:true, bytes:bytes.length, sha256:crypto.createHash('sha256').update(bytes).digest('hex')})); return;
  }
  if (['verify-public','decrypt-public'].includes(operation) && args.length === (operation === 'verify-public' ? 4 : 5)) {
    const privatePem = open(JSON.parse(readFile(inputFile)), privateKey(keyFile));
    const bytes = openPublic(JSON.parse(readFile(outputFile)), privatePem);
    if (operation === 'decrypt-public') { privateDirectory(args[4]); fs.writeFileSync(args[4], bytes, {mode:0o600, flag:'wx'}); }
    console.log(JSON.stringify({operation, verified:true, bytes:bytes.length, sha256:crypto.createHash('sha256').update(bytes).digest('hex')})); return;
  }
  throw new Error('用法：keygen KEY；recipient-key KEY PUBLIC_PEM PRIVATE_ENCRYPTED；encrypt KEY ENCRYPTED；encrypt-public PUBLIC_PEM ENCRYPTED < bytes；verify KEY ENCRYPTED；decrypt KEY ENCRYPTED PRIVATE_OUTPUT；verify-public KEY PRIVATE_ENCRYPTED ENCRYPTED；decrypt-public KEY PRIVATE_ENCRYPTED ENCRYPTED PRIVATE_OUTPUT');
}
module.exports = {seal, open, sealPublic, openPublic, privateKey};
if (require.main === module) main(process.argv.slice(2)).catch(() => { console.error('签名备份操作失败：检查参数、文件权限及恢复密钥；未输出秘密。'); process.exitCode = 1; });
