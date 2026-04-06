self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.airtag-tracker;
  package = self.packages.${pkgs.system}.server;
  extractorPkg = self.packages.${pkgs.system}.key-extractor;
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
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      systemd.services.airtag-tracker = {
        description = "AirTag location tracker";
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        wantedBy = [ "multi-user.target" ];

        environment = {
          AIRTAG_DATA_DIR = cfg.dataDir;
          AIRTAG_PORT = toString cfg.port;
          AIRTAG_POLL_INTERVAL = toString cfg.pollInterval;
        };

        serviceConfig = {
          ExecStart = "${package}/bin/airtag-tracker";
          DynamicUser = true;
          StateDirectory = "airtag-tracker";
          Restart = "on-failure";
          RestartSec = 10;
        };
      };
    }

    (lib.mkIf cfg.vm.enable {
      # KVM support for macOS VM
      boot.extraModprobeConfig = ''
        options kvm ignore_msrs=1
        options kvm report_ignored_msrs=0
      '';

      environment.systemPackages = [ pkgs.qemu ];

      # One-shot service to extract keys via macOS VM
      systemd.services.airtag-extract-keys = {
        description = "Extract AirTag keys from macOS VM";
        serviceConfig = {
          Type = "oneshot";
          ExecStart = "${extractorPkg}/bin/airtag-extract-keys";
          Environment = [
            "AIRTAG_VM_DIR=${cfg.vm.vmDir}"
            "AIRTAG_DATA_DIR=${cfg.dataDir}"
          ];
        };
      };
    })
  ]);
}
