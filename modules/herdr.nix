# Herdr — AI 编码代理的终端工作区管理器（NixOS 模块）
{ config, lib, pkgs, ... }:

let
  cfg = config.programs.herdr;
  tomlFormat = pkgs.formats.toml { };

  knownUsers = lib.filterAttrs (
    name: _:
    builtins.hasAttr name config.users.users
    && config.users.users.${name}.enable
  ) cfg.users;

  userTmpfiles = name: userCfg:
    let
      nixosUser = config.users.users.${name};
      configDir = "${nixosUser.home}/.config/herdr";
      configPath = "${configDir}/config.toml";
      source =
        if userCfg.configFile != null then
          userCfg.configFile
        else
          tomlFormat.generate "herdr-${name}-config.toml" userCfg.settings;
    in
    {
      "${nixosUser.home}/.config".d = {
        mode = "0700";
        user = nixosUser.name;
        group = nixosUser.group;
      };

      "${configDir}".d = {
        mode = "0700";
        user = nixosUser.name;
        group = nixosUser.group;
      };

      "${configPath}"."L+" = {
        user = nixosUser.name;
        group = nixosUser.group;
        argument = lib.replaceStrings [ "%" ] [ "%%" ] (toString source);
      };
    };
in
{
  options.programs.herdr = {
    enable = lib.mkEnableOption "Herdr terminal workspace manager";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The Herdr package to install system-wide; select an upstream package in the caller's private configuration.";
    };

    users = lib.mkOption {
      type = lib.types.attrsOf (lib.types.submodule {
        options = {
          settings = lib.mkOption {
            type = tomlFormat.type;
            default = { };
            description = ''
              Herdr settings written to the user's config file as TOML.

              WARNING: The generated file is stored in the world-readable Nix
              store. Do not put passwords, tokens, or other secrets here. Use
              `configFile` for a runtime secret-backed file instead.
            '';
          };

          configFile = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "/run/secrets-rendered/herdr-config.toml";
            description = ''
              Absolute runtime path to an existing Herdr TOML configuration.
              The path is used as an opaque symlink target; its contents are
              never imported into the Nix store. This supports paths produced
              by caller-provided secret management such as a sops template.
            '';
          };
        };
      });
      default = { };
      description = ''
        Per-user Herdr configuration. Each attribute name must identify a user
        declared in `users.users`; its config is linked at
        `<home>/.config/herdr/config.toml`.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    # 只遍历一次 cfg.users 汇总四项断言输入，四组断言仍按「先断言项、后用户」
    # 的原顺序拼接，config.assertions 的值逐项与原先一致。
    assertions =
      let
        checks = lib.mapAttrsToList (name: userCfg: {
          inherit name;
          defined = builtins.hasAttr name config.users.users;
          enabled =
            !builtins.hasAttr name config.users.users
            || config.users.users.${name}.enable;
          settingsEmptyOrConfigFile = userCfg.settings == { } || userCfg.configFile == null;
          configFileAbsolute = userCfg.configFile == null || lib.hasPrefix "/" userCfg.configFile;
        }) cfg.users;
      in
      map (c: {
        assertion = c.defined;
        message = "programs.herdr.users.${c.name} refers to an undefined NixOS user; define users.users.${c.name}.";
      }) checks
      ++ map (c: {
        assertion = c.enabled;
        message = "programs.herdr.users.${c.name} refers to a disabled NixOS user; enable users.users.${c.name}.";
      }) checks
      ++ map (c: {
        assertion = c.settingsEmptyOrConfigFile;
        message = "programs.herdr.users.${c.name}.settings and configFile are mutually exclusive; set only one.";
      }) checks
      ++ map (c: {
        assertion = c.configFileAbsolute;
        message = "programs.herdr.users.${c.name}.configFile must be an absolute runtime path.";
      }) checks;

    environment.systemPackages = [ cfg.package ];

    systemd.tmpfiles.settings."10-herdr" = lib.mkMerge (
      lib.mapAttrsToList userTmpfiles knownUsers
    );
  };
}
