# Windows Sandbox 原语

该包封装 Windows 专用账户、Token、Job Object、ACL 与 WFP。

- `api.py`：平台检查和延迟加载 Win32 模块。
- `accounts.py`：`WindowsAccountManager`、受限 Token。
- `jobs.py`：`WindowsJobObject`；`security.py`：`WindowsAclManager` 和 pipe security descriptor。
- `network.py`：`WindowsPowerShellWfpController` 与允许地址/端口补集。
- `__init__.py`：低层公开入口。

这些原语不包含业务审批；非 Windows 平台调用必须显式失败。

## 命令权限审计

`audit_policy.py` 使用 Windows API 启用对象访问失败、进程创建和登录结束审计，保留已有设置；全局文件和注册表审计 ACL 只追加沙箱账户，不修改访问权限。相关系统审计开关会增加安全日志。后端需要 `SeSecurityPrivilege` 和 Security 日志读取权限，缺失时禁止静默降级。

`permission_audit.py` 在启动前记录日志位置，启动后核对进程令牌的账户、登录 SID 和真实 AuthenticationId。使用 4688/4689 建立进程树和运行区间、4656 读取文件和注册表访问失败，清理进程树后等待 4634 确认登录结束。记录丢失、超限或结束确认超时均返回审计不可用；绝不匹配命令输出或事件描述文字。

`network_audit.py` 读取 5155/5157/5159 网络拒绝事件，必须同时匹配本次进程的运行区间，以及启动前读取的 Praxis provider/sublayer 下阻止规则的运行时 ID；其他防火墙规则不作为提权依据。系统需要开启 Filtering Platform Connection 失败审计。

本地代理使用每条命令独有的认证凭据，直接记录目标不在允许列表中的拒绝；禁网模式使用空允许列表。撤销凭据时原子取走记录并停止接收该身份的新记录。网站返回的 HTTP 403、DNS 或连接失败不产生代理策略拒绝记录。

业务层仅对失败且具有本次进程树拒绝记录的命令申请一次审批：拒绝保留首次错误，同意以当前 Windows 用户身份在沙箱外重跑一次，只返回第二次结果。并非申请 UAC 管理员权限。
