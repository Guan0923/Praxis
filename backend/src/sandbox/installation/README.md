# Broker 安装事务组件

该包保存提升权限的 Windows Broker 安装事务所需的纯合约、账户策略和 ACL 操作。外部进程入口仍是 `backend.sandbox.install_helper.main`，负责命令行输入校验和退出码。

- `contracts.py`：集中 `EXIT_*` 进程码、`TransactionFailure`、固定 Broker service class，以及 `validate_payload` 对不可信 CLI JSON 的路径、端口和服务命令校验。
- `accounts.py`：`provision_fixed_accounts` 创建或验证 `PraxisSbxOffline`/`PraxisSbxOnline` 与受管本地组，保留有效凭据，必要时更新并 DPAPI 持久化，授予最小登录权；不删除现有账户，归属不明时拒绝修改。
- `access_policy.py`：构造 ProgramData、敏感文件、source/runtime path 的 ACL 命令；`_SourceAclGrant` 描述精确 grant，`_iter_acl_tree` 在不跟随 reparse point 的前提下枚举声明树，`_secure_source_code` 和 `_apply_source_acl_grant` 对目录、既有子项及未来继承执行幂等的 Broker SID 只读/执行授权。
- `lock.py`：提升权限进程共用的 Windows 命名锁，覆盖宿主文件更新与服务操作，重复修复立即返回忙碌状态。
- `__init__.py`：仅重导出 installer 调用方需要的 ACL helper。

`install_helper.py` 负责 SCM 事务顺序和稳定门面，并通过显式 callback 把网络配置注入账户 provisioning，避免账户模块反向依赖事务入口。覆盖修复不删除旧安装目录、账户、就绪文件或旧版本遗留文件。目录权限通过对象句柄直接写入，只显式遍历 source/runtime 范围，不向无关子目录传播；足够的现有或继承权限不会重复添加。新就绪信息完整生成后替换同名文件，服务实际响应仍是健康判断的必要条件。路径校验必须发生在任何提升权限写操作之前；账户归属、ACL 或 WFP 状态无法证明时均失败关闭。
