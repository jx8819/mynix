# omp maxwork 全力模式扩展的 NixOS module。
# 机制公开（本仓库）；环境相关的值全部是 options，由使用方在自己的（私有）配置里赋值。
{ config, lib, pkgs, ... }:

let
  cfg = config.services.ompMaxwork;

  configJson = pkgs.writeText "maxwork.config.json" (builtins.toJSON {
    command = cfg.command;
    modelRoles = cfg.modelRoles;
    thinkingLevel = cfg.thinkingLevel;
    defaultMode = cfg.defaultMode;
    codex = {
      home = cfg.codex.home;
      outDir = cfg.codex.outDir;
      tmpdir = cfg.codex.tmpdir;
      timeoutSec = cfg.codex.timeoutSec;
    };
  });

  renderedCommand = pkgs.replaceVars ./extension/maxwork.md {
    agentDir = cfg.agentDir;
    command = cfg.command;
  };
in
{
  options.services.ompMaxwork = {
    enable = lib.mkEnableOption "omp maxwork 全力模式扩展（/maxwork）";

    agentDir = lib.mkOption {
      type = lib.types.str;
      example = "/mnt/Media/Settings/agent/omp";
      description = "omp 的 agent 目录（PI_CODING_AGENT_DIR）。扩展与命令文件安装到其 extensions/ 与 commands/ 下。";
    };

    owner = lib.mkOption {
      type = lib.types.str;
      default = "root";
      description = "安装文件的属主（应设为运行 omp 的用户）";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "root";
      description = "安装文件的属组";
    };

    command = lib.mkOption {
      type = lib.types.str;
      default = "maxwork";
      description = "斜杠命令名（/<command> 执行任务；/<command>-setup、/<command>-status 为管理命令）";
    };

    modelRoles = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "@slow" "@default" ];
      description = "「最高模型」角色回退链，按顺序取第一个已认证可用的角色（omp modelRoles 别名或 provider/id）";
    };

    thinkingLevel = lib.mkOption {
      type = lib.types.str;
      default = "max";
      description = "激活时设置的 thinking 级别";
    };

    defaultMode = lib.mkOption {
      type = lib.types.enum [ "omp" "codex" ];
      default = "codex";
      description = "默认模式：omp=仅 omp 内 API；codex=omp + codex CLI 联动（可被 /<command>-setup 覆盖，状态存 maxwork-state.json）";
    };

    codex = {
      home = lib.mkOption {
        type = lib.types.str;
        default = "";
        example = "/mnt/Media/Settings/agent/codex";
        description = "codex CLI 的 CODEX_HOME（codex 模式的可用性预检与委派使用；留空则预检总是失败并回退 omp）";
      };
      outDir = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "codex 委派产出文件的目录（协议文档用）";
      };
      tmpdir = lib.mkOption {
        type = lib.types.str;
        default = "/tmp";
        description = "codex 预检进程的 TMPDIR";
      };
      timeoutSec = lib.mkOption {
        type = lib.types.int;
        default = 300;
        description = "单个 codex 委派任务的超时秒数（协议文档用）";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    # agentDir 通常在持久数据盘上、不受 Nix 管理，用 activation 幂等安装。
    # maxwork-state.json 是用户运行时状态，不由本模块管理。
    system.activationScripts.ompMaxwork = ''
      install -D -m 0644 -o ${cfg.owner} -g ${cfg.group} ${./extension/maxwork.ts} ${cfg.agentDir}/extensions/maxwork.ts
      install -D -m 0644 -o ${cfg.owner} -g ${cfg.group} ${renderedCommand} ${cfg.agentDir}/commands/maxwork.md
      install -D -m 0640 -o ${cfg.owner} -g ${cfg.group} ${configJson} ${cfg.agentDir}/maxwork.config.json
    '';
  };
}
