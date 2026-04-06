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
      key-extractor = pkgs.callPackage ./extractor/package.nix {};
      provision-vm = pkgs.callPackage ./extractor/provision-package.nix {};
    });
  };
}
