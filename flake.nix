{
  description = "AirTag tracker for NixOS — track Apple AirTags from Linux with an Android-friendly web UI";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }: let
    systems = [ "x86_64-linux" ];
    forAllSystems = nixpkgs.lib.genAttrs systems;
  in {
    nixosModules.default = import ./module.nix self;

    packages = forAllSystems (system: let
      pkgs = nixpkgs.legacyPackages.${system};
    in {
      server = pkgs.callPackage ./server/package.nix {};
      provision-vm = pkgs.callPackage ./extractor/provision-package.nix {};
    });

    devShells = forAllSystems (system: let
      pkgs = nixpkgs.legacyPackages.${system};
      pythonEnv = pkgs.python3.withPackages (ps: with ps; [
        fastapi
        uvicorn
        pillow
        cryptography
        pytest
        httpx
        # opencv-python-headless not in nixpkgs by that name
        opencv4
      ]);
    in {
      default = pkgs.mkShell {
        name = "airtag-tracker-dev";
        buildInputs = [
          pythonEnv
          pkgs.qemu
          pkgs.tesseract
          pkgs.sshpass
          pkgs.git
          pkgs.python3Packages.websockify
          pkgs.novnc
        ];
        shellHook = ''
          export PYTHONPATH="$PWD/server:$PYTHONPATH"
          export AIRTAG_VM_ENABLED=true
          export AIRTAG_DATA_DIR=''${AIRTAG_DATA_DIR:-$HOME/airtag-dev}
          export AIRTAG_VM_DIR=''${AIRTAG_VM_DIR:-$HOME/airtag-dev/osx-kvm}
          export AIRTAG_VNC_WS_PORT=6901
          echo "AirTag dev shell ready. Run: python server/tracker.py"
        '';
      };
    });
  };
}
