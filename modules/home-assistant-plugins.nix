# Home Assistant custom components — declarative selection via options.
# 调用方（myconfig）只填 options，包和接线由本模块处理。
# 隐私/凭据请用 sops，不要写进本模块。
{ config, lib, pkgs, ... }:

let
  cfg = config.services.home-assistant-plugins;
in
{
  options.services.home-assistant-plugins = {
    enable = lib.mkEnableOption "Home Assistant custom components from mynix";

    dyson = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable Dyson purifier/fan integration (libdyson-wg/ha_libdyson).";
    };

    cololight = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable Cololight LED integration.";
    };

    xiaomiHome = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Enable XiaoMi/ha_xiaomi_home integration (official Xiaomi Miot).
        Uses nixpkgs home-assistant-custom-components.xiaomi_home.
        Requires extraComponents: ffmpeg, zeroconf.
      '';
    };

    xiaomiMiot = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable al-one/xiaomi_miot integration (alternative to xiaomiHome).";
    };
  };

  config = lib.mkIf cfg.enable {
    services.home-assistant = {
      customComponents =
        lib.optional cfg.xiaomiHome pkgs.home-assistant-custom-components.xiaomi_home
        ++ lib.optional cfg.xiaomiMiot pkgs.home-assistant-custom-components.xiaomi_miot
        ++ lib.optional cfg.dyson pkgs.ha-dyson
        ++ lib.optional cfg.cololight pkgs.ha-cololight;

      # xiaomi_home 依赖 ffmpeg + zeroconf（见 nixpkgs package 注释）
      extraComponents =
        lib.optionals cfg.xiaomiHome [ "ffmpeg" "zeroconf" ]
        ++ lib.optionals cfg.dyson [ "bluetooth" ]
        ++ lib.optionals cfg.cololight [ ];
    };
  };
}
