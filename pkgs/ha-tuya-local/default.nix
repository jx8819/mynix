# ha-tuya-local — 复用 Nixpkgs 的 tuya_local，追加产品描述和必要的上游修复。
# 主机地址、设备 ID、local_key 与配置条目由站点在运行时提供。
{ home-assistant-custom-components }:

home-assistant-custom-components.tuya_local.overrideAttrs (previousAttrs: {
  # options 更新必须经过 HA core 的锁、卸载/加载状态机和状态通知。
  patches = (previousAttrs.patches or [ ]) ++ [
    ./0001-reload-through-config-entry-manager.patch
  ];

  postInstall = (previousAttrs.postInstall or "") + ''
    install -m444 ${./fresh_air_dehumidifier_gcej0ye8rj60qiee.yaml} \
      "$out/custom_components/tuya_local/devices/fresh_air_dehumidifier_gcej0ye8rj60qiee.yaml"
  '';
})
