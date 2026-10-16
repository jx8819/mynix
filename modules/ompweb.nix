# ompweb — OMP 编码代理的 Web UI（NixOS 模块）。
# 机制全部公开；主机相关的值都是 options，由调用方在私有配置里赋值。
{ config, lib, pkgs, ... }:

let
  cfg = config.services.ompweb;
in
{
  options.services.ompweb = {
    enable = lib.mkEnableOption "ompweb Web UI";

    hostname = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "监听地址。只应在本机反代（caddy/nginx 带 TLS+认证）后暴露；0.0.0.0 会明文暴露密码。";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 30177;
      description = "Port to listen on";
    };

    user = lib.mkOption {
      type = lib.types.str;
      description = "User to run ompweb as";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      description = "Data persistence directory";
    };

    agentDir = lib.mkOption {
      type = lib.types.path;
      description = "OMP agent directory (sessions, memories, config)";
    };

    extraReadWritePaths = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "主机特定的额外可写路径（实例参数，由调用方配置，勿写进本模块）";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "是否在防火墙开放 port。仅当确需绕过反代直连时才开；对外入口应统一走反代（TLS + 密码）。";
    };

    environmentFile = lib.mkOption {
      type = lib.types.str;
      description = "Path to a private EnvironmentFile supplying OMP_WEB_PASSWORD.";
    };
  };

  config = lib.mkIf cfg.enable {
    # 用户通常已在别处定义为 normal user，这里不重复创建；只需要确保组存在。
    users.groups.${cfg.user} = lib.mkIf (!builtins.hasAttr cfg.user config.users.users) { };

    systemd.tmpfiles.rules = [
      "d ${cfg.dataDir} 0750 ${cfg.user} ${cfg.user} -"
      "d ${cfg.agentDir} 0750 ${cfg.user} ${cfg.user} -"
    ];

    systemd.services.ompweb = {
      description = "OMP Web UI";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" "sops-install-secrets.service" ];
      wants = [ "network-online.target" ];
      environment = {
        OMP_WEB_HOSTNAME = cfg.hostname;
        OMP_WEB_PORT = toString cfg.port;
        OMP_WEB_NO_OPEN = "1";
        PI_CODING_AGENT_DIR = cfg.agentDir;
        HOME = cfg.dataDir;
        PATH = lib.mkForce (lib.concatStringsSep ":" [ "${pkgs.nodejs}/bin" "/run/current-system/sw/bin" config.security.wrapperDir ]);
      };
      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.user;
        Restart = "always";
        RestartSec = 5;
        EnvironmentFile = cfg.environmentFile;
        ExecStart = "${pkgs.ompweb}/bin/ompweb --hostname ${cfg.hostname} --port ${toString cfg.port}";
        # 只补建缺失的 SQLite 文件：不改写既有 OMP 状态，改属主不跟随
        # 密钥管理装配的 config.yml 软链。root 前置脚本用于绕过 tmpfiles
        # 对外部数据挂载盘的 unsafe-path 检查。
        ExecStartPre = "+${pkgs.writeShellScript "ompweb-initialize-state" ''
          set -eu
          agent_dir=${lib.escapeShellArg (toString cfg.agentDir)}
          ${pkgs.coreutils}/bin/install -d -m 0750 -o ${lib.escapeShellArg cfg.user} -g ${lib.escapeShellArg cfg.user} "$agent_dir"

          for name in agent.db models.db; do
            file="$agent_dir/$name"
            if [ ! -e "$file" ] && [ ! -L "$file" ]; then
              ( umask 027; set -C; : > "$file" )
            fi
          done

          for name in config.yml models.yml agent.db models.db; do
            file="$agent_dir/$name"
            if [ -f "$file" ] && [ ! -L "$file" ]; then
              ${pkgs.coreutils}/bin/chown ${lib.escapeShellArg "${cfg.user}:${cfg.user}"} "$file"
              ${pkgs.coreutils}/bin/chmod 0640 "$file"
            fi
          done
        ''}";
        WorkingDirectory = cfg.dataDir;
        # 本服务定位是「管理入口」，防护边界应在网络与认证层（反代密码/TLS），
        # 本单元沙箱仅作防误操作；如需收紧请按主机威胁模型自行加固。
        NoNewPrivileges = false;
        ProtectSystem = "strict";
        ProtectHome = "read-only";
        ReadWritePaths = [ cfg.dataDir cfg.agentDir ] ++ cfg.extraReadWritePaths;
        SupplementaryGroups = [ "keys" ];
      };
    };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}
