self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.airtag-tracker;
  package = self.packages.${pkgs.system}.server;
  extractorPkg = self.packages.${pkgs.system}.key-extractor;
  provisionPkg = self.packages.${pkgs.system}.provision-vm;
in {
  options.services.airtag-tracker = {
    enable = lib.mkEnableOption "AirTag location tracker";

    port = lib.mkOption {
      type = lib.types.port;
      default = 8042;
      description = "Port for the web UI and API.";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/airtag-tracker";
      description = "Directory for keys, database, and account session.";
    };

    pollInterval = lib.mkOption {
      type = lib.types.int;
      default = 900;
      description = "Seconds between location polls (default: 15 min).";
    };

    vm = {
      enable = lib.mkEnableOption "macOS VM for AirTag key extraction";

      vmDir = lib.mkOption {
        type = lib.types.path;
        default = "/var/lib/airtag-tracker/osx-kvm";
        description = "Path to OSX-KVM directory with macOS disk image.";
      };

      vncPort = lib.mkOption {
        type = lib.types.port;
        default = 5901;
        description = "VNC port for macOS VM display.";
      };

      websocketPort = lib.mkOption {
        type = lib.types.port;
        default = 6901;
        description = "WebSocket port for noVNC proxy.";
      };

    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      users.users.airtag-tracker = {
        isSystemUser = true;
        group = "airtag-tracker";
        home = cfg.dataDir;
      };
      users.groups.airtag-tracker = {};

      systemd.services.airtag-tracker = {
        description = "AirTag location tracker";
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        wantedBy = [ "multi-user.target" ];

        environment = {
          AIRTAG_DATA_DIR = cfg.dataDir;
          AIRTAG_PORT = toString cfg.port;
          AIRTAG_POLL_INTERVAL = toString cfg.pollInterval;
          AIRTAG_VM_ENABLED = lib.boolToString cfg.vm.enable;
          AIRTAG_VM_DIR = cfg.vm.vmDir;
          AIRTAG_VNC_WS_PORT = toString cfg.vm.websocketPort;
        };

        serviceConfig = {
          ExecStart = "${package}/bin/airtag-tracker";
          User = "airtag-tracker";
          Group = "airtag-tracker";
          StateDirectory = "airtag-tracker";
          Restart = "on-failure";
          RestartSec = 10;
        };
      };
    }

    (lib.mkIf cfg.vm.enable {
      boot.extraModprobeConfig = ''
        options kvm ignore_msrs=1
        options kvm report_ignored_msrs=0
      '';

      environment.systemPackages = [ pkgs.qemu ];

      # One-time VM provisioning — downloads macOS + OSX-KVM, creates disk.
      systemd.services.airtag-provision-vm = {
        description = "Provision macOS VM for AirTag key extraction";
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        wantedBy = [ "multi-user.target" ];
        unitConfig.ConditionPathExists = "!${cfg.vm.vmDir}/mac_hdd_ng.img";
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          User = "airtag-tracker";
          Group = "airtag-tracker";
          ExecStart = "${provisionPkg}/bin/airtag-provision-vm";
          Environment = [ "AIRTAG_VM_DIR=${cfg.vm.vmDir}" ];
        };
      };

      # noVNC websocket proxy — bridges browser to VM's VNC display.
      # Started/stopped on demand by the tracker server.
      systemd.services.airtag-novnc = {
        description = "noVNC WebSocket proxy for macOS VM";
        serviceConfig = {
          Type = "simple";
          ExecStart = "${pkgs.python3Packages.websockify}/bin/websockify --web ${pkgs.novnc}/share/webapps/novnc 127.0.0.1:${toString cfg.vm.websocketPort} localhost:${toString cfg.vm.vncPort}";
          Restart = "on-failure";
        };
      };

      # Periodic key extraction — boots VM, extracts new keys, shuts down.
      systemd.services.airtag-extract-keys = {
        description = "Extract AirTag keys from macOS VM";
        unitConfig.ConditionPathExists = [
          "${cfg.vm.vmDir}/mac_hdd_ng.img"
          "${cfg.dataDir}/vm-password"
        ];
        serviceConfig = {
          Type = "oneshot";
          User = "airtag-tracker";
          Group = "airtag-tracker";
          ExecStart = "${extractorPkg}/bin/airtag-extract-keys";
          Environment = [
            "AIRTAG_VM_DIR=${cfg.vm.vmDir}"
            "AIRTAG_DATA_DIR=${cfg.dataDir}"
          ];
          TimeoutStartSec = "15min";
        };
      };

    })
  ]);
}
