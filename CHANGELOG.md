# Changelog

## [1.2.0](https://github.com/jamestrichardson/mantis/compare/mantis-v1.1.0...mantis-v1.2.0) (2026-09-16)


### Features

* **deploy:** add explicit-tag deploy and rollback for degobah ([#62](https://github.com/jamestrichardson/mantis/issues/62)) ([4d4f557](https://github.com/jamestrichardson/mantis/commit/4d4f557b01d0f2e752743d8461dd989474cfe4ef)), closes [#59](https://github.com/jamestrichardson/mantis/issues/59)

## [1.1.0](https://github.com/jamestrichardson/mantis/compare/mantis-v1.0.0...mantis-v1.1.0) (2026-09-16)


### Features

* **release:** build and publish linux/amd64 + linux/arm64 images ([2a6646e](https://github.com/jamestrichardson/mantis/commit/2a6646e3a0351fccf2d0aa880786a1d3e840bd6c))
* **release:** publish a pr-&lt;number&gt; image on pull requests ([4330b21](https://github.com/jamestrichardson/mantis/commit/4330b2108c20eaaceaf1db4a6f7b5a12cad4d5e5))
* **release:** publish container images to GHCR ([0517bca](https://github.com/jamestrichardson/mantis/commit/0517bca28ca2df34c836b2e86a773187b8d2e8f3))
* **release:** publish container images to GHCR ([#43](https://github.com/jamestrichardson/mantis/issues/43)) ([f40ede3](https://github.com/jamestrichardson/mantis/commit/f40ede334dc6afb4531c9f01b74c4b0fdb413f2d))

## [1.0.0](https://github.com/jamestrichardson/mantis/compare/mantis-v0.1.0...mantis-v1.0.0) (2026-09-16)


### ⚠ BREAKING CHANGES

* INITIAL COMMIT

### Features

* **ci:** add GitHub Actions CI for tests, package build, and container build ([2d44326](https://github.com/jamestrichardson/mantis/commit/2d4432645cf402d32ac0b43c65007026c106f689))
* **ci:** add GitHub Actions CI for tests, package build, and container build ([#41](https://github.com/jamestrichardson/mantis/issues/41)) ([d3f5681](https://github.com/jamestrichardson/mantis/commit/d3f5681f083ae956fb12d515f1b607926a7faf09))
* **contracts:** add shared tool-result provenance/error contract ([#23](https://github.com/jamestrichardson/mantis/issues/23)) ([585b64c](https://github.com/jamestrichardson/mantis/commit/585b64cb7635f05f249a445f5bf2b2fd515955a7))
* **contracts:** add shared tool-result provenance/error contract ([#23](https://github.com/jamestrichardson/mantis/issues/23)) ([84ad3ab](https://github.com/jamestrichardson/mantis/commit/84ad3abb321556d216acbd2552531948a10dd03f))
* **eval:** add deterministic scoring for evaluation scenarios ([#36](https://github.com/jamestrichardson/mantis/issues/36)) ([f6d3c68](https://github.com/jamestrichardson/mantis/commit/f6d3c6852595728e7bf107dcaa44f423d6bcde2b))
* **eval:** add evaluation harness skeleton and model-runner CLI ([#34](https://github.com/jamestrichardson/mantis/issues/34)) ([64f3241](https://github.com/jamestrichardson/mantis/commit/64f324151e5b279897892d555cf2908a6fe00789))
* **eval:** add evaluation harness skeleton and model-runner CLI ([#34](https://github.com/jamestrichardson/mantis/issues/34)) ([85ff703](https://github.com/jamestrichardson/mantis/commit/85ff70377c7ae0e9fb3fe1f46941dbeb1f9ee449))
* **eval:** deterministic scoring with hard/quality checks ([#36](https://github.com/jamestrichardson/mantis/issues/36)) ([cad689c](https://github.com/jamestrichardson/mantis/commit/cad689c25371ad3079adc3b40d13686688213d70))
* INITIAL COMMIT ([0abcd86](https://github.com/jamestrichardson/mantis/commit/0abcd864f56477fcf1f1010a4ad9cdb20dcfd4fe))
* **release:** automate SemVer releases with release-please ([04e2918](https://github.com/jamestrichardson/mantis/commit/04e29187953c38a8b2a15ea7fd5696b02a8f498b))
* **release:** automate SemVer releases with release-please ([#42](https://github.com/jamestrichardson/mantis/issues/42)) ([9a9a883](https://github.com/jamestrichardson/mantis/commit/9a9a8830997b3c3ab742edda91c3bb922d0727d9))


### Bug Fixes

* **config:** redact credentials and test missing-variable failures ([12382e9](https://github.com/jamestrichardson/mantis/commit/12382e9bc608e6436238e236348f30777d5e6b52))
* **config:** redact credentials and test missing-variable failures ([2b90dfb](https://github.com/jamestrichardson/mantis/commit/2b90dfbe938838016d6be005ddabacfb0591ad1c))
* **contracts:** close observation-time and evidence/interpretation gaps ([#23](https://github.com/jamestrichardson/mantis/issues/23)) ([5697e19](https://github.com/jamestrichardson/mantis/commit/5697e196c51f52036e77728a74b62241ce5d2fa2))
* **eval:** stop misattributing Mantis bugs to models; add empty-answer diagnostics ([b16b8d5](https://github.com/jamestrichardson/mantis/commit/b16b8d54c1eb52a45a4ab50ce4df591e6055cca6))
* **runtime:** stop starving the model on duplicate tool calls and fix slow completions ([3895ee7](https://github.com/jamestrichardson/mantis/commit/3895ee7f897c6650c140ad4cbc3c2c1aeef811ef))
* **runtime:** stop starving the model on duplicate tool calls and fix… ([ee64d7e](https://github.com/jamestrichardson/mantis/commit/ee64d7e462685249a1947ffed5fc807398c2c49d))
