# Changelog

## [1.6.0](https://github.com/jamestrichardson/mantis/compare/mantis-v1.5.0...mantis-v1.6.0) (2026-09-18)


### Features

* **agent:** add System Troubleshooter multi-source investigation agent ([00a1443](https://github.com/jamestrichardson/mantis/commit/00a144316d44613efa40caf54d04e9aea6deb6fe))
* **agent:** add System Troubleshooter multi-source investigation agent ([f67c527](https://github.com/jamestrichardson/mantis/commit/f67c5273fa1e028343180c9f198975b6351456d6))
* **kubernetes:** add read-only Kubernetes inspection tools ([5cfbefa](https://github.com/jamestrichardson/mantis/commit/5cfbefa04f154674ac07bca6cf3cd86264b2c104))
* **kubernetes:** add read-only Kubernetes inspection tools ([a73a84d](https://github.com/jamestrichardson/mantis/commit/a73a84dd73bf506315f02ee0632660ea20ed7a83))


### Bug Fixes

* **agent:** small eval/config consistency fix ([2983bca](https://github.com/jamestrichardson/mantis/commit/2983bca15106db975cb68d5ab14e3e0a189c4831))
* **kubernetes:** propagate nested truncation, classify auth failures ([a54b222](https://github.com/jamestrichardson/mantis/commit/a54b222aea7780bdb64b66ed7fc360f4f3341812))

## [1.5.0](https://github.com/jamestrichardson/mantis/compare/mantis-v1.4.0...mantis-v1.5.0) (2026-09-17)


### Features

* **awx:** add structured job-event failure inspection tool ([db9d026](https://github.com/jamestrichardson/mantis/commit/db9d026b89b9ec7fad33c282787d12a674a48d8b))
* **awx:** add structured job-event failure inspection tool ([3712d30](https://github.com/jamestrichardson/mantis/commit/3712d309be97034de1688081a6994ac896d927ec)), closes [#28](https://github.com/jamestrichardson/mantis/issues/28)
* **loki:** add Loki integration and bounded log-query tool ([bb80de7](https://github.com/jamestrichardson/mantis/commit/bb80de7539539c43f91ab3769ce54f2955a25141))
* **loki:** add Loki integration and bounded log-query tool ([2d707b1](https://github.com/jamestrichardson/mantis/commit/2d707b16b61c7bb6936195f6ab1da19ea2412e5e))
* **network:** add reusable host reachability and TCP connectivity tools ([491149e](https://github.com/jamestrichardson/mantis/commit/491149e59532828dbf2788b2a642e60629423992))
* **network:** add reusable host reachability and TCP connectivity tools ([0e88b97](https://github.com/jamestrichardson/mantis/commit/0e88b974a32e4f23ee9e639f076d8a67232c3dc3)), closes [#8](https://github.com/jamestrichardson/mantis/issues/8)
* **prometheus:** add Prometheus integration and query tool ([9cf51df](https://github.com/jamestrichardson/mantis/commit/9cf51df908d58f1528a5322f0c6249fb568f752a))
* **prometheus:** add Prometheus integration and query tool ([4d42c1e](https://github.com/jamestrichardson/mantis/commit/4d42c1eeda6010d958d7372ba9d356b1da7c8963)), closes [#9](https://github.com/jamestrichardson/mantis/issues/9)
* **reliability:** establish bounded timeouts, retries, failure taxonomy, and run budgets ([0fe484e](https://github.com/jamestrichardson/mantis/commit/0fe484e70dd67107370acecb18a43bd09af4471a))
* **reliability:** establish bounded timeouts, retries, failure taxonomy, and run budgets ([6933fef](https://github.com/jamestrichardson/mantis/commit/6933fefa9f6d0e3cec20c83f8710bd2aa34bfe7b)), closes [#15](https://github.com/jamestrichardson/mantis/issues/15)


### Bug Fixes

* **awx:** correct truncation/inspection-cap semantics, wire tool into AWX Troubleshooter ([b85a294](https://github.com/jamestrichardson/mantis/commit/b85a294f194a559d260b3331054b988f8b34f008))
* **loki:** narrow start/end schema to RFC3339-only, reject unrepresentable timestamps before HTTP ([0ab2a12](https://github.com/jamestrichardson/mantis/commit/0ab2a12e3713e33f86691c9e794334f692991948))
* **loki:** reflect malformed warnings shape in meta.truncated ([01d5d3e](https://github.com/jamestrichardson/mantis/commit/01d5d3eaff1d3e82f1d309453993fcb4810b153c))
* **loki:** source-side limit sentinel for truthful truncation, guard malformed warnings shape ([3fde36d](https://github.com/jamestrichardson/mantis/commit/3fde36d690c5d70e56a22b0633bf7017d4befa93))
* **network:** report budget_exceeded when deadline stops remaining candidates after a partial failure ([37d4edc](https://github.com/jamestrichardson/mantis/commit/37d4edcbb6a6b43b48bdeeb6b08abf412ce14c4a))
* **prometheus:** bound invalid-input echo and sample values, fix query-error truncation truthfulness ([606c1a5](https://github.com/jamestrichardson/mantis/commit/606c1a519b3010bbc1d04cbe09b962206a61b734))
* **prometheus:** fix raw-count-before-filtering bug and malformed result-container masquerading as empty ([d014229](https://github.com/jamestrichardson/mantis/commit/d01422976089ac4815e9c72d7723c0573980f462))
* **prometheus:** fix raw-count-before-filtering bug and malformed result-container masquerading as empty ([fd4e257](https://github.com/jamestrichardson/mantis/commit/fd4e25711006c2aa16f8e8115565ad583716cd29))
* **prometheus:** fix truncation blind spots, warning-count cap, and non-finite input validation ([d859435](https://github.com/jamestrichardson/mantis/commit/d859435154b2eea19da465179c15730da9996383))
* **reliability:** close breaker blind spot and harden config validation ([e922d5f](https://github.com/jamestrichardson/mantis/commit/e922d5f578d2bce924bf5d9dae2689b9ea0fd688))
* **reliability:** stop within-call hammering once the breaker opens, reject non-finite config, fix docs claim ([a53c57f](https://github.com/jamestrichardson/mantis/commit/a53c57fde3f68fb9e085a9e626f08c2d8d68e71d))

## [1.4.0](https://github.com/jamestrichardson/mantis/compare/mantis-v1.3.1...mantis-v1.4.0) (2026-09-17)


### Features

* **security:** establish untrusted tool-output trust boundary ([be789ef](https://github.com/jamestrichardson/mantis/commit/be789eff11423544144b64a49f32dc2afb7d2e96))
* **security:** establish untrusted tool-output trust boundary ([#14](https://github.com/jamestrichardson/mantis/issues/14)) ([594df4a](https://github.com/jamestrichardson/mantis/commit/594df4a616d8f3bd111cd4295a54dd5d064080b2))


### Bug Fixes

* **security:** make MODEL_TOOL_RESULT_MAX_CHARS a true ceiling, guarantee serializable output ([012dd81](https://github.com/jamestrichardson/mantis/commit/012dd812b9f98e414ed178466c72460d1377d868))

## [1.3.1](https://github.com/jamestrichardson/mantis/compare/mantis-v1.3.0...mantis-v1.3.1) (2026-09-17)


### Bug Fixes

* **observability:** stop enabling metrics server by default in the container ([ed38d73](https://github.com/jamestrichardson/mantis/commit/ed38d73c6cab815e67d81c37a97b19db0ac93374))
* **observability:** stop enabling metrics server by default in the container ([f60f586](https://github.com/jamestrichardson/mantis/commit/f60f586fbf37e69036dd1d171e3ad21be3fa8abf))

## [1.3.0](https://github.com/jamestrichardson/mantis/compare/mantis-v1.2.0...mantis-v1.3.0) (2026-09-17)


### Features

* **observability:** structured JSON logging and Prometheus metrics ([2a3d3ff](https://github.com/jamestrichardson/mantis/commit/2a3d3ff9333b57212e8021854ac6d7efaa452721))
* **observability:** structured JSON logging and Prometheus metrics ([#38](https://github.com/jamestrichardson/mantis/issues/38), [#39](https://github.com/jamestrichardson/mantis/issues/39)) ([08c50ab](https://github.com/jamestrichardson/mantis/commit/08c50abd938bb1a0437c55d93344dace494dec59))

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
