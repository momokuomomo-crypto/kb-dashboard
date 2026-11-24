# 週末の買い目（期待値派×騎手）

`weekend.py` が、週末の全レース（重賞・非重賞・新馬戦）から買い目を出し、
リポジトリ直下の `picks.html` / `picks.json` に書き出す（GitHub Pagesで公開）。
実行は `.github/workflows/weekend_picks.yml`（金・土20時、土・日9時・12時 JST）。

* 学習：`jra-archive` ブランチの確定済み全レースで、kb-project の期待値派（04_07）を学習
  （`analysis_engine/` は kb-project scripts/04_analysis/analysis_engine のコピー）
* 買い目：期待値1.0以上・単勝50倍未満・M.デムーロ/C.デムーロ/ルメール/川田の騎乗馬の複勝、
  1点1,000円、週末合計5,000円まで（期待値順）。複勝オッズ下限1.3倍未満は買わない（トリガミ防止）
* 参考表示（買わない）：武豊・岩田康誠・団野大成・戸崎圭太・田辺裕信
* 根拠：全5,256レースの検証で前半108%（82点）・後半111%（98点）。確定オッズでの検証

テスト：`cd picks && python -m unittest discover -s tests`
