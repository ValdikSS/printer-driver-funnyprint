{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };
  outputs =
    { self, nixpkgs, ... }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
      ];
    in
    {
      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt-tree);
      packages = forAllSystems (system: {
        default = self.packages.${system}.funnyprint;
        funnyprint =
          let
            pkgs = nixpkgs.legacyPackages.${system};
            lib = nixpkgs.lib;
            pname = "funnyprint";
            version = "1.0-unstable-2025-10-09";
            src = ./.;
            funnyprint-py = pkgs.python3Packages.buildPythonPackage {
              inherit pname version src;
              pyproject = true;

              build-system = with pkgs.python3Packages; [ setuptools ];

              dependencies = with pkgs.python3Packages; [ bleak ];

              meta.mainProgram = "rastertofunnyprint";
            };
          in
          pkgs.stdenvNoCC.mkDerivation {
            inherit pname version src;
            dontBuild = true;
            installPhase = ''
              runHook preInstall
              mkdir -p $out/lib/cups/backend
              install -Dm444 xiqi.drv $out/share/cups/drv/funnyprint-xiqi.drv
              ln -s ${lib.getExe funnyprint-py} $out/lib/cups/backend/funnyprint
              runHook postInstall
            '';
          };
      });
      nixosModules = {
        default = self.nixosModules.funnyprint;
        funnyprint =
          { pkgs, ... }:
          {
            services.printing = {
              enable = true;
              drivers = [ self.packages.${pkgs.system}.funnyprint ];
            };
          };
      };
    };
}
