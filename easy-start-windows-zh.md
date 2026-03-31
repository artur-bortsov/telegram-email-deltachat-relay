# Aardvark — Windows 快速入门

几分钟内即可将 Telegram 频道的消息转发至 Delta Chat 和/或电子邮件。

---

## 所需准备

1. **Aardvark 安装包** — 下载并解压到任意文件夹
2. **Telegram API 凭据** — API ID 和 API Hash
3. **电子邮件账号** — 用于通过 Delta Chat 或直接邮件转发发送消息（如有需要）
4. **Telegram 频道名称** — 需要转发的频道

---

## 第 1 步 — 获取 Telegram API 凭据

1. 在浏览器中打开 <https://my.telegram.org/apps>
2. 使用 Telegram 账号的手机号码登录
3. 创建一个应用（名称任意，例如 "Aardvark"）
4. 复制 **api_id**（数字）和 **api_hash**（32 位字符串）

---

## 第 2 步 — 准备电子邮件账号 *（如有需要）*

如需通过 Delta Chat 或直接邮件进行投递，请准备一个专用电子邮件地址，仅用于发送转发消息。  
建议使用单独的地址，而非个人邮箱。

---

## 第 3 步 — 运行安装程序

打开解压后的安装包文件夹，双击：

```
installers\windows\install.cmd
```

或以管理员身份从命令提示符运行：

```cmd
installers\windows\install.cmd
```

安装程序将启动交互式配置向导。请输入：

- **API ID** 和 **API Hash**（来自第 1 步）
- Telegram 账号的手机号码
- 需要转发的频道 — 例如 `@channelname` 或数字频道 ID
- Delta Chat 和/或电子邮件转发设置（如有需要）

其余参数可保留默认值。

---

## 第 4 步 — 确认 Telegram 登录

首次运行时，Telegram 会向您填写的手机号码发送**短信验证码**。  
请在安装程序的终端窗口中输入该验证码。

如果账号已启用**两步验证（Cloud Password / 2FA）**，短信验证码输入后将立即出现密码提示——请在同一窗口输入密码。

登录成功后，会话将保存至 `.session` 文件。  
此后每次启动服务时将自动使用该会话——无需再次输入验证码。

---

## 第 5 步 — 分享 Delta Chat 邀请链接

服务启动后，每个 Delta Chat 广播频道的邀请链接将出现在以下文件中：

```
C:\Program Files\Aardvark\invite_links.txt
```

请通过**安全渠道**（如 Signal 或加密邮件）将这些链接分享给订阅者。  
订阅者需在 Delta Chat 应用中打开链接，才能开始接收消息。

---

## 服务控制

```cmd
sc query   AardvarkRelay
sc start   AardvarkRelay
sc stop    AardvarkRelay
```

完整文档请参阅[主 README](README.md)。
