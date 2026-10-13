# Apple 中间证书获取

公开源码只保留[受信 manifest](../signing/resources/apple-intermediate-certificates/manifest.txt) 的官方 URL
和固定 DER SHA-256，不重新授权或分发七份 Apple PEM。管理员使用
[获取脚本](../signing/fetch_apple_intermediates.py) 下载到私有目录，再沿用原签名脚本的 `verify`：

```bash
python3 signing/fetch_apple_intermediates.py --output-dir /srv/ci/apple-ca-reviewed
APPLE_INTERMEDIATE_CERTIFICATE_DIR=/srv/ci/apple-ca-reviewed \
  bash signing/ios_apple_intermediate_certificates.sh verify
```

父目录须已存在、由当前管理员持有、不可由其他用户写入且无符号链接；输出目录必须不存在。
脚本默认验证 TLS，只使用 manifest 中 `https://www.apple.com/certificateauthority/` 的 URL，
不跟随重定向，限制响应体和下载时间。七份 DER 与转换后 PEM 的 DER 摘要全部匹配后，才无覆盖地原子发布。
命令不安装证书，也不操作钥匙串。

## 间歇 HTTP 403 的处理

2026-10-13 在本机重新获取时，一次全套请求于 G6 返回 curl 56，输出目录没有发布；
随后用完全相同的脚本、官方 URL 和摘要重新执行，七份下载全部成功。此前失败的 Developer ID G2
在本机和已有 Jenkins 服务器网络也都返回 HTTP 200，并与固定摘要一致。
[Apple 官方 PKI 页面](https://www.apple.com/certificateauthority/) 仍列出这些证书。

这些结果排除了固定摘要不匹配和官方 URL 永久不可用；失败表现为间歇网络拒绝。
本机有 HTTPS 代理变量，服务器无这些变量；直连也曾返回 403 后恢复 200。
现有失败记录没有保留拒绝方的响应头，不能据此确定是代理节点、出口策略还是 Apple 服务端拒绝。
无需增加镜像域名、放宽 TLS、改摘要或复制旧 PEM。

失败后检查输出目录没有发布，可以用同一命令重新获取。重试仍须全套完成所有校验；
持续失败时保留退出状态并检查当前网络路径，不使用 `--insecure`、任意代理或重定向绕过。
已成功发布的目录不能用同一路径覆盖。

