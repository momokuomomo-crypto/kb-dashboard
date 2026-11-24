# JRA全レース収集（kb-project 06_08）

JRAの公開データ（全レースの結果・払戻金・確定オッズ・出走馬の血統）を集めて、
非公開リポジトリ kb-project の `jra-archive` ブランチに保存する（データはこの公開リポジトリには置きません）プログラムです。予想ロジックは非公開リポジトリ（kb-project）にあります。
設計・収集状況・再開方法は kb-project の `docs/06_pipeline/06_08_全レースアーカイブ設計.md` を参照。

- 実行: `.github/workflows/jra_archive.yml`（毎週火曜7:30 JST、手動実行も可）
- 公開リポジトリなのでGitHub Actionsの利用時間は無料（非公開のkb-projectでは月2,000分の無料枠を使い切ったため移設）
- テスト: `cd collector && python -m pytest tests`
