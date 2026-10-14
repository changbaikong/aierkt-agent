# 事故与阶段 1 技术结论（2026-09-23）

## 一、事故：AppData 目录权限被覆盖（已修复）

**原因**：做 Win32 验证脚本时用了 `SetEntriesInAclW(1, &ea, NULL, &acl)` + `SetNamedSecurityInfoW`。
第三个参数传 `NULL` 表示旧 ACL 为空 —— 结果是**整份权限表被替换成我这一条 ACE**，
不是"追加一条"。脚本给 `task` / `base` 授权时照此执行，后来遍历父目录时把
`%LOCALAPPDATA%`（`C:\Users\<用户名>\AppData\Local`）、`%APPDATA%`（`C:\Users\<用户名>\AppData`）
的权限表一起清空。

**波及范围**：只有上面两个目录。用户主目录（所有者是 SYSTEM，我没有改权限的资格）、
`C:\Users`、`C:\` 都没被改动；AppData 的子目录因为那条 ACE 没带继承标记，没有传播。

**修复**：`icacls "…\AppData" /reset /T /C`，恢复为从上级继承的默认权限表。
修复后两个目录都是标准的 SYSTEM / Administrators / 当前用户 全控制，读写正常。

**遗留症状**：WorkBuddy 的 bash 通道随后启动失败，报错
`LsSDK_CreateSandbox → CreateProcessW(bash.exe) → ERROR_ACCESS_DENIED`。
已排除的：AppContainer 本身正常（新建 profile、在容器里 `CreateProcessW` 都成功）；
`AppData` / `AppData\Local` / `Packages\cr.sb.*` / `.workbuddy` 的权限表都是标准形态
（用新建一个 AppContainer 存储目录做对照，权限表完全一致）。
推断：常驻的 `sandbox-cli.exe` / `sandbox-center.exe` 在权限表被清空的那段时间里缓存了坏状态，
**重启 WorkBuddy 应可恢复**。

## 二、阶段 1 的真实技术约束（已实测，需写进实现）

1. **AppContainer 进程必须拿到 `LOCALAPPDATA`**。用 SECURITY_CAPABILITIES 属性 + 自定义环境块时，
   环境块里缺 `LOCALAPPDATA` → `CreateProcessW` 直接返回 203（ERROR_ENVVAR_NOT_FOUND）。
   实测该变量**取任何值都可以**（沙箱内路径、甚至不存在的路径都行），所以填沙箱内路径即可，
   不必暴露宿主真实路径。
2. **阶段 1 不使用子进程策略属性，统一由 Job 活动进程上限 1 强制禁止子进程**。
   后续隔离测试确认：属性号 14（`0x0002000E`）传 DWORD `0` 会允许 Python 创建
   子进程；传 `PROCESS_CREATION_CHILD_PROCESS_RESTRICTED`（DWORD `1`）又会让普通
   Python、PowerShell 和 `cmd.exe` 在初始化阶段直接失败。因此不能把 `0` 误写成安全
   边界，也不能为了使用该属性牺牲受管运行时可用性。阶段 1 的真实强制机制是先把暂停
   进程加入活动进程上限为 1 的 Job Object，再恢复执行；真实 Python 和 PowerShell
   攻击测试必须证明创建子进程失败。
3. **运行时目录不能用硬链接准备**。`CreateHardLink` 需要**源文件**的写属性权限，
   `C:\Program Files` 下的文件普通用户没有 → WinError 5。所以要**复制**
   （PowerShell 7 约 300 MB，实测 24 秒）。Python 装在使用者目录下硬链接可用，建议统一用复制。
4. **ACL 授权必须与既有权限表合并**（上面事故的根因）：`SetEntriesInAclW` 第三个参数必须传现有 DACL；
   撤销时只删本 Attempt 的 SID 那一条 ACE。
5. 命令行、环境块、句柄白名单、Job Object 的组合已验证可用；`STARTUPINFOEXW` 大小 112。
