# omp — can1357/oh-my-pi 编码代理：包安装 + 运行时数据约定的 NixOS module
# 机制全部公开；主机相关的值都是 options（由调用方在私有配置里赋值）。
#
# 设计假设：运行时身份固定 root，不引入 agent 用户概念。
# 敏感值（API key/token/模型密钥）禁止写进本仓库与 Nix store：
# 由调用方经 sops.secrets / sops.templates 注入，本模块的私密配置
# option 一律接受 path/string 引用（如 config.sops.secrets.<name>.path），默认 null。
{ config, lib, pkgs, ... }:

let
  cfg = config.programs.omp;

  dataDirs = [ cfg.homeDir cfg.agentDir ] ++ lib.optional (cfg.codexHome != null) cfg.codexHome;

  envFileLoad = "set -a; . ${lib.escapeShellArg (toString cfg.environmentFile)}; set +a";

  wrapped = pkgs.runCommand "omp-wrapped" { nativeBuildInputs = [ pkgs.makeWrapper ]; } ''
    mkdir -p $out/bin
    makeWrapper ${lib.getExe cfg.package} $out/bin/omp \
      --set PI_CODING_AGENT_DIR ${lib.escapeShellArg (toString cfg.agentDir)} \
      --set HOME ${lib.escapeShellArg (toString cfg.homeDir)} \
      ${lib.optionalString (cfg.codexHome != null) "--set CODEX_HOME ${lib.escapeShellArg (toString cfg.codexHome)}"} \
      ${lib.concatStringsSep " " (lib.mapAttrsToList (k: v: "--set ${k} ${lib.escapeShellArg v}") cfg.extraEnvironment)} \
      --run 'umask 077' \
      ${lib.optionalString (cfg.environmentFile != null) "--run ${lib.escapeShellArg envFileLoad}"}
    if [ -d ${cfg.package}/share ]; then
      ln -s ${cfg.package}/share $out/share
    fi
  '';
in
{
  options.programs.omp = {
    enable = lib.mkEnableOption "omp 编码代理（can1357/oh-my-pi）";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.omp;
      defaultText = lib.literalExpression "pkgs.omp";
      description = "omp 包（本仓库 overlay 提供 pkgs.omp）";
    };

    agentDir = lib.mkOption {
      type = lib.types.path;
      description = ''
        OMP agent 数据目录（wrapper 导出 PI_CODING_AGENT_DIR）：config.yml、
        models.yml、mcp.json、agent.db/models.db、skills/memories/sessions 等
        都在此目录下。
      '';
    };

    homeDir = lib.mkOption {
      type = lib.types.path;
      description = ''
        运行时 HOME（wrapper 导出）：可写凭据/缓存目录所在，与 agentDir 同根。
      '';
    };

    codexHome = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        CODEX_HOME（codex 互操作目录，wrapper 导出）；null 表示不导出。
      '';
    };

    configFile = lib.mkOption {
      type = lib.types.nullOr (lib.types.either lib.types.path lib.types.str);
      default = null;
      description = ''
        config.yml 来源：设置后软链到 $agentDir/config.yml。
        API key/token 类的值禁止写默认值——默认 null；如文件含敏感内容，
        由调用方经 sops.secrets 注入（传 config.sops.templates."<name>".path
        或 config.sops.secrets."<name>".path），禁止把明文写进本仓库或 Nix store。
      '';
    };

    modelCatalogFile = lib.mkOption {
      type = lib.types.nullOr (lib.types.either lib.types.path lib.types.str);
      default = null;
      description = ''
        模型目录 models.yml 来源（模型 API key/token 载体）：设置后软链到
        $agentDir/models.yml。API key/token 类的值禁止写默认值——默认 null；
        由调用方经 sops.secrets 注入：传 config.sops.templates."<name>".path
        （sops 渲染到 tmpfs，明文不进本仓库/Nix store）。
      '';
    };

    mcpConfigFile = lib.mkOption {
      type = lib.types.nullOr (lib.types.either lib.types.path lib.types.str);
      default = null;
      description = ''
        mcp.json 来源（如 pkgs.writeText 生成的 MCP server 配置）：设置后软链到
        $agentDir/mcp.json。command/env 可引用 store 路径；含凭据的值禁止写进
        本仓库，敏感值经 environmentFile 传递。
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr (lib.types.either lib.types.path lib.types.str);
      default = null;
      description = ''
        运行时敏感环境变量文件（KEY=VALUE，shell 可 source）：模型/API 的
        API key/token 类值经此注入，默认 null；由调用方经 sops.secrets 注入
        （传 config.sops.secrets."<name>".path 或 sops.templates 渲染路径）。
        wrapper 启动前加载，明文不进 Nix store；文件缺失时 wrapper 直接报错退出
        （fail-closed，凭据载体不允许无密钥静默启动）。
      '';
    };

    extraEnvironment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "wrapper 导出的额外非敏感环境变量。敏感值禁止经此传明文，改用 environmentFile。";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ wrapped ];

    # 数据目录与 omp 配置软链在每次 switch 生效（NixOS 的 tmpfiles 只在 boot 执行）。
    # 软链目标是 sops 渲染路径时允许先悬空，渲染后即生效。
    system.activationScripts.omp = lib.stringAfter [ "users" ] ''
      install -d -m 0700 -o root -g root ${lib.concatMapStringsSep " " (d: lib.escapeShellArg (toString d)) dataDirs}
      ${lib.optionalString (cfg.configFile != null) ''
        ln -sfn ${lib.escapeShellArg (toString cfg.configFile)} ${lib.escapeShellArg "${toString cfg.agentDir}/config.yml"}
      ''}
      ${lib.optionalString (cfg.modelCatalogFile != null) ''
        ln -sfn ${lib.escapeShellArg (toString cfg.modelCatalogFile)} ${lib.escapeShellArg "${toString cfg.agentDir}/models.yml"}
      ''}
      ${lib.optionalString (cfg.mcpConfigFile != null) ''
        ln -sfn ${lib.escapeShellArg (toString cfg.mcpConfigFile)} ${lib.escapeShellArg "${toString cfg.agentDir}/mcp.json"}
      ''}
    '';
  };
}
