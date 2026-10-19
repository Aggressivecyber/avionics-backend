# 兼容性固定样本

`v0.2-correlation.avbus` 是基线 `0f00e927424f73fe5deb07b7da41aab13527ecf1` 原程序产生的 v1 格式确定性关联样本，17 条记录、1769 字节。

SHA-256：`9aa8b26d6a6c6979a2d9718e9a59a9ea2c2d68a676a781f5dbd7708212792368`。

它没有会话身份或结束封存，读取时必须保留 `legacy_unverified`，不能把旧记录可解析等同于原始采集完整。
