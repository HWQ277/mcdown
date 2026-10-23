# PCL / HMCL 的 `.minecraft` 结构、版本隔离与独立加载器版本

本文记录这次修改所依据的实际实现，而不是凭经验猜测。研究时间为 2026-10-23。

## 最重要的约定

用户选择的文件夹**就是** `.minecraft` 根目录。工具不会再创建一层 `<版本号>/`：

```text
你选择的文件夹/                  # 这就是 .minecraft
├── assets/
├── libraries/
└── versions/
    └── <版本 ID>/
        ├── <版本 ID>.json
        ├── <版本 ID>.jar
        └── ...该版本的数据...
```

PCL 和 HMCL 都共享根目录下的 `assets/`、`libraries/`；版本隔离只改变每个版本的游戏运行目录，不会把这些共享目录搬进版本文件夹。

## HMCL

### 标准路径

HMCL 的 `DefaultGameRepositoryLayout` 明确使用：

- 版本目录：`versions/<id>/`
- 版本 JSON：`versions/<id>/<id>.json`
- 原版 JAR：`versions/<id>/<id>.jar`
- 共享库：`libraries/`
- 共享资源：`assets/`，索引在 `assets/indexes/<assetId>.json`，对象在 `assets/objects/xx/<hash>`

来源：[DefaultGameRepositoryLayout.java](https://github.com/HMCL-dev/HMCL/blob/main/HMCLCore/src/main/java/org/jackhuang/hmcl/game/DefaultGameRepositoryLayout.java)。

### 版本隔离

HMCL 官方文档给出的隔离结构：

```text
.minecraft/
├── assets
├── libraries
└── versions
    └── 1.16.5-optifine
        ├── 1.16.5-optifine.jar
        ├── mods
        ├── saves
        ├── resourcepacks
        ├── logs
        └── ...
```

来源：[HMCL 文档：全局版本隔离](https://docs.hmcl.net/launcher/isolation.html)。

HMCL 的 `HMCLGameRepository#getRunDirectory()` / `computeRunDirectory()` 逻辑是：
实例设置中的 `runningDirectory` 为空，且 `overrideProperties` 包含该字段时，运行目录就是
`versions/<id>/`。来源：

- [HMCLGameRepository.java](https://github.com/HMCL-dev/HMCL/blob/main/HMCL/src/main/java/org/jackhuang/hmcl/game/HMCLGameRepository.java)
- [HMCLGameRepositoryLayout.java](https://github.com/HMCL-dev/HMCL/blob/main/HMCL/src/main/java/org/jackhuang/hmcl/game/HMCLGameRepositoryLayout.java)

每个实例的本地设置位于：

```text
versions/<id>/.hmcl/config/instance-game-settings.json
```

本工具写入：

```json
{
  "$schema": "https://schemas.glavo.site/hmcl/instance-game-settings/1.0.0",
  "icon": "FABRIC",
  "overrideProperties": ["runningDirectory"],
  "runningDirectory": ""
}
```

`1.0.0` 对应当前正式版 HMCL 3.16.4；新版 1.0.1 仍可读取旧 schema。字段定义见
[instance-game-settings/1.0.0.json](https://github.com/HMCL-dev/HMCL/blob/main/docs/schemas/instance-game-settings/1.0.0.json)。

## PCL

### 标准路径与版本隔离

PCL 源码定义：

```vb
PathVersion = McFolderSelected & "versions\" & NameOrFullPath
```

版本隔离开启时：

```vb
PathIndie = PathVersion
```

启动参数中 `${game_directory}` 使用 `PathIndie`，而 `${assets_root}` 仍然指向共享的
`McFolderSelected & "assets"`。来源：

- [ModMinecraft.vb：PathVersion / PathIndie](https://github.com/Meloong-Git/PCL/blob/main/Plain%20Craft%20Launcher%202/Modules/Minecraft/ModMinecraft.vb#L236-L277)
- [ModLaunch.vb：game_directory / assets_root](https://github.com/Meloong-Git/PCL/blob/main/Plain%20Craft%20Launcher%202/Modules/Minecraft/ModLaunch.vb#L1516-L1527)
- [ModMinecraft.vb：assets 路径](https://github.com/Meloong-Git/PCL/blob/main/Plain%20Craft%20Launcher%202/Modules/Minecraft/ModMinecraft.vb#L2011-L2032)

PCL 的版本级设置文件：

```text
versions/<id>/PCL/Setup.ini
```

格式为 `Key:Value`，CRLF；读取逻辑见
[Settings.vb](https://github.com/Meloong-Git/PCL/blob/main/Plain%20Craft%20Launcher%202/Pages/PageSetup/Settings.vb#L241-L272)，
冒号写入格式见
[ModBase.vb](https://github.com/Meloong-Git/PCL/blob/main/Plain%20Craft%20Launcher%202/Modules/Base/ModBase.vb#L536-L560)。

本工具写入：

```ini
VersionArgumentIndie:1
VersionArgumentIndieV2:True
```

这样 PCL 的 `PathIndie` 直接使用 `versions/<id>/`，即使 `mods` / `saves` 暂时为空也不会退回根目录。

PCL 将包含 `versions/` 的文件夹识别为 Minecraft 游戏目录，见
[ModMinecraft.vb](https://github.com/Meloong-Git/PCL/blob/main/Plain%20Craft%20Launcher%202/Modules/Minecraft/ModMinecraft.vb#L64-L70)。
因此用户选择的文件夹不需要命名为 `.minecraft`。

## 为什么加载器版本可以不再保留原版版本

原版启动器格式允许加载器 JSON 使用 `inheritsFrom` 继承原版。本工具安装完成后做的是：

1. 把原版 JSON 与加载器 JSON 合并；
2. 把原版客户端 JAR 复制到加载器目录，命名为 `<加载器实例>.jar`；
3. 删除 `inheritsFrom`，设置新的 `id` 和 `jar`；
4. 删除这次安装临时创建的原版版本目录（如果安装前已经存在原版，则保留）。

### HMCL 的继承合并语义

HMCL 的 `GameInstanceManifest#merge(parent)` 明确实现：

- `arguments`：父级参数在前、子级参数在后；
- `libraries`：子级（加载器）库在前、父级库在后；
- `mainClass`、`assets`、`assetIndex`、`javaVersion` 等：子级优先，缺失时使用父级；
- 合并结果的 `inheritsFrom` 为 `null`。

见
[GameInstanceManifest.java](https://github.com/HMCL-dev/HMCL/blob/main/HMCLCore/src/main/java/org/jackhuang/hmcl/game/GameInstanceManifest.java#L66-L90)。
本工具的合并顺序与之一致，因此合并后的 JSON 是标准独立版本。

### PCL 对独立版本的识别

PCL 的 `McVersion` 在没有 `inheritsFrom` 时仍能识别原版版本：

- Forge/NeoForge 从 `arguments.game` 中的 `--fml.mcVersion` 读取；
- Fabric/Quilt 从 `net.fabricmc:intermediary` 等库版本读取；
- 加载器类型从版本 JSON 中的 `net.fabricmc:fabric-loader`、`net.minecraftforge`、
  `net.neoforge` 等字符串识别。

见
[McVersion.cs](https://github.com/Meloong-Git/PCL/blob/main/PCLCS/Minecraft/McVersion.cs#L113-L178)。
因此把原版信息合并进加载器 JSON 后，PCL 不依赖额外的原版版本目录。

## 最终生成的结构

### 只下载原版

```text
你选择的 .minecraft/
├── assets/
│   ├── indexes/
│   ├── objects/
│   └── log_configs/
├── libraries/
└── versions/
    └── 1.20.1/
        ├── 1.20.1.jar
        └── 1.20.1.json
```

### 安装加载器（新目录）

```text
你选择的 .minecraft/
├── assets/
├── libraries/
├── versions/
│   └── fabric-loader-0.19.5-1.20.1/
│       ├── fabric-loader-0.19.5-1.20.1.json   # 已合并，无 inheritsFrom
│       ├── fabric-loader-0.19.5-1.20.1.jar    # 原版客户端 JAR
│       ├── mods/
│       ├── saves/
│       ├── resourcepacks/
│       ├── shaderpacks/
│       ├── config/
│       ├── logs/
│       ├── PCL/Setup.ini
│       └── .hmcl/config/instance-game-settings.json
├── launcher_profiles.json
└── .mcdown/
    └── installers/                            # 安装器和日志
```

`versions/` 下没有额外的 `1.20.1/` 原版目录；加载器版本就是唯一的游戏版本。

如果安装前已经存在原版版本，工具不会删除它，只把加载器版本做成独立版本。这样可以避免误删已有的存档、Mod 或配置。

## 实际验证

- 用官方 **HMCL 3.16.4** JAR 分别扫描本工具生成的 Fabric、Forge、NeoForge、Quilt
  独立版本目录；
- HMCL 日志每次都只加载一个版本，没有加载临时原版版本，也没有出现实例设置 schema 错误；
- 四种加载器的安装都通过了工具内的结构自检：加载器 JSON 无 `inheritsFrom`、
  JAR 存在、PCL/HMCL 隔离配置存在；
- PCL 是 Windows 程序，本次没有在 Wine 中启动其二进制；PCL 部分按当前 GitHub
  源码核对 `PathVersion`、`PathIndie`、`PCL/Setup.ini`、`McVersion` 的完整路径和解析逻辑。

## 资源文件说明

本次修复的是**目录结构、版本隔离和独立加载器版本**。

`assets/indexes` 与 `assets/objects` 目录已创建，但完整资源对象文件没有下载；
`libraries/` 会包含加载器安装所下载的依赖。PCL 和 HMCL 都有资源补全/修复功能，可用于补齐
缺失的 assets 和原版运行库。如果你需要完全离线启动，应另外补齐完整 assets 和原版 libraries。
