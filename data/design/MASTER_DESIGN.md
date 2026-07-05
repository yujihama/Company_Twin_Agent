# 統制環境ツイン 方針書・全体設計

**対象**: 規程・マニュアル・ワークブック等の統制文書から「統制環境のデジタルツイン」を構築し、複数の役割エージェントに文書を解釈させて自由にふるまわせ、条件をfuzzing的に揺らした多数の並列実行から、解釈差・証跡不足・例外濫用・検知漏れ・統制副作用を観察・定量化するシステム。

**初期ドメイン**: ダミーフィナンシャルホールディングス（DFH）販売プロセス文書群 v1.1（50文書、2026-07-02版）。旧版zip（2026-07-01版）を事実上のv1.0として版ずれ素材に使用する。

**本書の位置づけ**: 実装・実験・レビューに関わる全員（人間・エージェント）の共通参照点。本書と個別成果物（コンパイル報告書、span registry、world_config等）が矛盾する場合は本書を優先し、本書を改定する。

---

## 1. 目的と到達点

### 1.1 何を作るか

次の4つのサブシステムを作る。

1. **環境コンパイラ**: 統制文書群を入力に、実行可能な世界（コーパス・カーネル・検索面・人口・デッキ）を生成する半自動パイプライン。
2. **世界実行系（ハーネス）**: 役割エージェントが文書を読み、解釈し、行為する世界を、再現可能な形で多数並列に実行する系。
3. **観測系**: 世界内で起きた全事実を実験者だけが見える形で記録し（flight recorder）、オラクル群で所見に変換する系。
4. **トリアージ系**: 数百ラン×数千イベントを、バケット化・最小再現・統計処理を通じて人間がレビュー可能な量に圧縮する系。

### 1.2 何ができるようになるか（検証テーマ）

文書解釈差の検証、規程間矛盾の行動影響、証跡期待のズレ、例外運用の観察、非公式プロセス（口頭・チャット承認）の発生、統制fuzzing、統制変更の副作用観察、既存検知ルールの限界測定、業務横断の汎化、監査仮説の生成。これらは全て「事象の有無」ではなく「条件別の発生率と信頼区間」として出力される（§11.4）。

### 1.3 何を作らないか

- 世界の中で正解を判定する審判（Game Master）。
- 規程内容をif-then化したルールエンジン。
- 全トランスクリプトをLLMが読んで要約するレビュー機構。

これらを作らない理由は§3の設計原則から導かれる。

---

## 2. 用語定義

解釈揺れを防ぐため、本書では以下の語を厳密に使い分ける。

| 用語 | 定義 |
|---|---|
| **世界（world）** | 1回のシミュレーションが走る閉じた環境。コーパス・カーネル・人口・デッキ・スケジュール・seedの組で完全に定義される。世界間で共有される可変状態は存在しない。 |
| **世界内プレーン** | エージェントが見る・触れることのできる全て。文書、システムツール、アーティファクト、チャネル、他エージェントの行為。 |
| **実験者プレーン** | エージェントから不可視の全て。flight recorder、変異記録、span registry、潜在真実、解釈レコード、オラクル、分析基盤。 |
| **カーネル** | 世界内のITの物理を再現する決定論コード。ステータス機械・権限・不可変タイムスタンプ・時限イベントのみを扱い、規程の意味内容を一切知らない。 |
| **ハード制約（SYSTEM）** | カーネルが物理的に強制する制約。破るにはシステム改修が要るもの。 |
| **ソフト規範（HUMAN）** | 文書が要求するが、人がその気になれば破れるもの。文書のまま世界に置き、コード化しない。 |
| **knob** | ハード/ソフトの判定が文書上決まらない、またはITGC依存度の実験対象としたい制約。世界プロファイルでon/offを切り替える。 |
| **アーティファクト** | 世界内に存在する記録物（申込レコード、承認レコード、チェックシート、チャットログ等）。行為＝アーティファクトの作成・編集として定義される。 |
| **座席（seat）** | 世界内の1エージェントポジション。役割・ツール束・検索プロファイル・モデル束縛・tick予算を持つ。 |
| **tick** | 世界時間の最小刻み（半日）。tickごとにカーネルが時限イベントを発火し、inboxが非空の座席が起床する。 |
| **tick予算** | 1座席が1tickで使えるツールコール数の上限。効率勾配（時間圧）の実装。 |
| **デッキ** | 事前確定された刺激の列。販売ドメインでは顧客ペルソナ＋来訪意図＋潜在真実の組。 |
| **潜在真実** | 顧客等の内的状態（本当の理解度・実年齢・家族の意向等）。実験者プレーンに事前コミットされ、世界には顧客の言動としてのみ現れる。 |
| **プローブ** | 特定のseeded spanの解釈を強制するよう設計されたデッキ項目。被覆行列でspanと対応づける。 |
| **seeded span** | コンパイル時に登録された曖昧文言・矛盾箇所。span registry（実験者プレーン）で管理し、世界には一切の印を付けない。 |
| **解釈レコード（basis）** | 統制関連行為の際にエージェントが記録する構造化根拠（参照文書・版・span・読み・却下した代替案・確信度）。カーネルが行為から剥離し実験者プレーンへ送る。世界には行為だけが出る。 |
| **world_config** | 世界を完全に定義する1枚の宣言。fuzzingの入力単位。 |
| **アンサンブル** | 同一world_configをseedだけ変えてK回実行した集合。確率的なLLM挙動に対する最小観測単位。 |
| **対照ペア** | Δ=1フィールドのみ異なり、デッキとseedを完全共有する2つのworld_config。帰属分析の単位。 |
| **アンカーラン** | 無変異・全knob-off・固定seedの基準世界。毎バッチに同梱し、ハーネス・モデルのドリフトを検知する。 |
| **シグネチャ／バケット** | オラクル所見を正規化ハッシュした識別子と、同一シグネチャの所見の束。 |
| **min-repro** | バケットを再現する最小の世界（変異1・プローブ1・座席2〜3・数tick）と再現率の組。 |
| **S0/S1/S2** | 三段ハーネスの各段。S0=静的解釈バッテリ（シミュレーションなし）、S1=取引1件のエピソードラン、S2=フル世界。 |
| **diegetic** | 実験条件を「世界内の物」（通達メール、締切、過去チケット）として与えること。プロンプトへの直接注入の対義。 |

---

## 3. 設計原則（不変条件）

以下は実装・実験を通じて破ってはならない原則である。各原則に「破るとどうなるか」を付す。

### P1. シリコンが強制するものだけコード化する

判定テスト: 「その制約を破るのに、人が規程を破れば足りるか（→ソフト）、システム改修が要るか（→ハード）」。文書上不明な場合は**ソフトに倒してknob登録**する（fail-open）。

理由: 誤ってコード化すると観察対象（解釈差・例外・非公式代替）がコンパイル時に消え、事後検出できない。誤ってコード化しないと「現実で不可能な行為」が起きるが、recorderに全行為が残るため事後にフラグ・再実行できる。誤りの非対称性により、可視な誤り（fail-open）だけを許容する。

### P2. 二平面の分離を守る

世界内に真理・審判・実験意図を置かない。span registry・潜在真実・変異記録・解釈レコードは実験者プレーン専用であり、いかなる形でも世界内文書・プロンプト・ツール応答に混入させない。合成物にはリークlint（新鮮なモデルに「この文書群から実験の存在を推測できるか」を敵対質問）を必須とする。

破ると: エージェントが「試験を受けている」と気づき、行動が評価適応化して忠実性が崩壊する。

### P3. 条件操作は全てdiegeticに与える

圧力は締切とキュー滞留で、監視認識は監査通知メールで、慣行は過去チケット・過去チャットで、版ずれは索引配信の非対称で与える。ペルソナプロンプトに「あなたは監視されている」「雑に読む性格だ」等を書かない。

理由: プロンプト操作は人格と条件を交絡させ、D条件の帰属分析（何が効いたか）を壊す。

### P4. 世界のデフォルト応答は沈黙

規程違反の行為をしても、警報は鳴らず、支払は落ち、世界は何も言わない。エージェントへの帰結は(a)ハード制約の失敗、(b)他エージェントの反応（差戻し・照会・指摘）の2種のみ。

理由: 「応答が来ない逸脱」こそが観察対象。世界が何か言う設計はこの沈黙を壊す。

### P5. エージェント分化の第一手段は情報環境、性格ではない

役割ごとの解釈差は、役割別検索プロファイル（索引収載・ランキング偏向・top-k）と参照習慣・目的関数の緊張・tick予算から機械的に生じさせる。ロールカードには責務・KPI・証跡観・時間予算のみを書き、**規程の内容を一切書かない**（CIでコーパスとの規範内容重複をlintする）。

理由: ロールカードに規範を書いた瞬間、ルールベースの密輸となり、「文書を読んで解釈する」という観察対象が消える。

### P6. 再現性はハーネスの決定論化で確保する

検索順・デッキ順・顧客ペルソナ・リゾルバ裁定（メモ化）をseed固定し、LLM本体の確率性だけをアンサンブルで受ける。所見は必ず「config×K seedsの発生率」として扱い、単発事象を所見にしない。

### P7. 世界はcattle、petsではない

世界はworld_config 1枚から完全再生成できること。手作業で世界を修繕しない。修繕が必要ならconfigまたはコンパイラを直す。

### P8. LLMの読解量は新規性に比例させる

全ラン全文をLLMに読ませない。決定論・構造オラクルが全件を処理し、LLM（解釈オラクル）は新規バケットの代表例のみを読む。

---

## 4. 全体アーキテクチャ

```
┌──────────────────── 実験者プレーン ────────────────────┐
│ 環境コンパイラ(Stage0-9) → span registry / 変異演算子 / デッキ+潜在真実   │
│ fuzzingスケジューラ(world_config生成・昇格判定・カバレッジ地図)          │
│ flight recorder(attempts/basis/ledger) → オラクルL0/L1/L2 → バケット     │
│ トリアージ(min-repro/統計/4ビュー) → 監査仮説カード                     │
└──────────────┬───────────────────────────────┘
               │ world_config(1枚) × N並列
┌──────────────┴─── 世界内プレーン(世界ごとに独立) ──────────┐
│ 文書コーパス(生テキスト+envelope) ← 役割別検索面                        │
│ カーネル(ステータス機械/権限/時限イベント/tick予算)                      │
│ アーティファクト/チャネル(システムツール+非公式チャネル)                 │
│ 座席エージェント(deepagents) + 顧客エージェント(デッキ駆動・最小モデル)   │
└─────────────────────────────────────────┘
```

構成要素の責務分界:

| 要素 | 知っていること | 知らないこと |
|---|---|---|
| カーネル | ステータス・権限・時刻・必須フィールド | 金額の意味・適合性・高齢者・規程の内容 |
| 検索面 | 索引・ランキング・版 | 正しい解釈 |
| 座席エージェント | ロールカード＋世界内で読んだもの＋自分の記憶 | span registry・潜在真実・実験の存在 |
| 顧客エージェント | ペルソナ＋潜在真実の演技指示 | 規程コーパス全体 |
| recorder/オラクル | 全事実 | （世界へ介入しない） |

---

## 5. 環境コンパイル・パイプライン

文書の山→動く世界への変換。コンパイル時のエージェント（強モデル）はルールベース解析を自由に行ってよい。**成果物が世界に入るのではなく、人間レビュー可能なデータになる**ためである（GMの知性を実行時から構築時へ移す、が基本思想）。

| Stage | 内容 | 成果物 | DFH v2での確定事項 |
|---|---|---|---|
| 0 | 棚卸しとenvelope付与（doc_id/版/施行日/文書区分/権威水準/可視範囲）。メタデータは同一性とアクセスの記述であり、**意味の記述ではない**。権威水準（規程3>マニュアル2>ワークブック1）は定義するだけで優先解決には使わない | corpus manifest | 50文書、全v1.1。旧zipをv1.0として登録 |
| 1 | 世界の名詞抽出（役割・アーティファクト種別・システム・チャネル・周期イベント・組織）。**存在のみ、ルールは抽出しない** | 語彙骨格 | 25システム・7担当者(A-G)・判断5値等 |
| 2 | 行為面の抽出（アーティファクト種別ごとの操作列挙） | ツール面候補 | 起票/承認/差戻/記入/取消/照会等 |
| 3 | **決定論境界の判定**。パス1: 規範文をLLMで構造抽出（modality/actor/action/condition/引用span）。パス2: P1のテストでSYSTEM/HUMAN/knobに分類。norms.jsonlは世界に入れないが捨てず、structural oracleの座標系にする | norms.jsonl＋boundary_decisions | SYSTEM=イベントID採番・承認必須フィールド・期限超過自動通知・ステータス機械。knob=チェックシートゲート/資格ゲート/SoD/資料ピッカー/完了条件ゲート |
| 4 | カーネル仕様生成（宣言YAML→性質テスト: 全状態到達可能・デッドエンドなし） | kernel_spec | §6参照 |
| 5 | 検索面構築（役割別プロファイル）＋**検索監査**（典型クエリで意図した順位が出るか計測してから使う） | retrieval_profiles | 担当者=文書内FAQ・現場判断事例ブースト、規程は摩擦大の全文検索経由でのみ到達 |
| 6 | 曖昧性採掘（語彙パス＋多モデル冷読＋矛盾採掘）。全spanに解釈候補集合を注釈（分類用・実験者プレーン専用） | span registry | AMB-01/02/04d/08/09/10/11/12、CONTRA-01、STR-01/02r、SCC-01 |
| 7 | 慣行層の合成（組織レジスタ→FAQ・通達・過去チケット・過去チャット。driftプロファイルは基底率を登録パラメータ化。リークlint必須） | 合成コーパス | v2は文書内FAQ・現場判断事例を持つため外部合成は過去チケット・チャット中心に縮小 |
| 8 | デッキと潜在真実の事前確定。被覆行列で全seeded spanにプローブを対応づけ、潜在真実はrecorderへ事前コミット | deck＋coverage matrix | P-01〜P-10（routine 28件含む） |
| 9 | 妥当性ゲート（§12） | ゲート判定 | backcasting正例=現場判断事例ミラー45×4件 |

**コンパイル経済の注意**: v2コーパス（49万字・重複5%）は全読不可能である。機械パス全量→標的LLM読解の二段構えを標準とし、機械パスの検出限界（平叙義務文「〜する。」はデオンティック・パターンで拾えない）を認識してStage 3パス1は必ずLLMで行う。

**実文書が使えない場合**: 構造統計（文書数・相互参照グラフ・デオンティック密度・曖昧語密度・版履歴深さ）だけを実コーパスから測り、内容は架空社で再生成する（構造移植）。忠実性主張は「内容一致」でなく「統計一致」に置く。

---

## 6. カーネルと世界実行系

### 6.1 ステータス機械（申込）

状態: 下書き→申込受付→本人確認済→審査連携中→契約成立→書面交付済（分岐: 差戻し／取消撤回／販売見送り。取消撤回は追記型イベントで上書き不可）。

ハードガード: 申込受付→審査連携中は「eKYC完了∧同意ログID非空∧制裁非ヒット」を要求。審査は環境サービス（2営業日遅延・確率的差戻し）としてコード実装する。

### 6.2 knobとプロファイル

| knob | off（erp_standard・既定） | on（erp_strict） | 根拠span |
|---|---|---|---|
| K-checksheet-gate | 要承認入力でも確定可 | 管理者承認まで確定ブロック | 048記入ルール |
| K-qualification-gate | 無資格でも起票ツール可 | 資格フラグ連動 | 教育・資格管理 |
| K-sod-gate | 販売担当者が申込確定可 | 申込確定は申込担当のみ | 実務手順の分掌 |
| K-material-picker | 全版選択可＋差戻し運用 | 承認済み版のみ選択可 | AMB-10（仕様自己矛盾の両読みを両世界化） |
| K-completion-gate | 証跡なしでも完了可 | 同意ログ・録音ID・資料版数を完了条件化。**ラン中日付で切替可** | SCC-01（050改善4） |

`spreadsheet`プロファイル: ステータス機械なし・/world生マウント・履歴はrecorderのみ。ITGC最弱世界としてカーネル梯子の一端を成す。

### 6.3 スケジューラとtick予算

tick=半日。時限イベント: 日次inbox配信、W2金キャンペーン締切、W3水木の管理者不在、W4末月次締め（050集計要求）、承認期限超過の自動通知、SCC-01切替日。tick予算（例: 販売担当者14コール/tick）は効率勾配の唯一の実装であり、文書精読とキュー消化を同一予算で競合させる。

### 6.4 通信の制約

座席間通信は世界アーティファクト（チャット・メール・ワークフロー）経由のみとし、**エージェント同士のsubagent呼び出しを禁止する**。subagentは一つの精神の内部（例: 監査人の私的作業）に限って許可する。理由: エージェント間の直接呼び出しは証跡を残さない通信となり、証跡十分性の測定を壊す。

---

## 7. 役割エージェント設計

### 7.1 座席構成（DFH pack v0）

販売担当者4（emp-A投信/アプリ、emp-B保険/面談、emp-F加盟店/BtoB、emp-G口座/アプリ＝049のA-G準拠）、申込担当1（emp-C）、販売管理者1（emp-M、W3水木不在）、第二線1（emp-Q品質管理部）、世界内監査1、顧客はデッキ駆動。**監査は二重化**する: 世界内監査アクター（解釈差測定用）と、事後の証跡再構成プローブ（証跡十分性測定用・別インスタンス）を分け、相互汚染を防ぐ。

### 7.2 ロールカードの内容規律

書いてよいもの: 責務、KPIの緊張（処理量vs正確性）、証跡観（何が残っていれば安心か）、参照習慣（初手でどこを見るか）、時間予算、リスク感度。書いてはならないもの: 規程・マニュアルの内容、spanへの言及、実験の存在。CIで自動lint。

### 7.3 解釈レコード（basis）

統制タグ付きツール（承認実行・チェックシート記入・申込確定・例外申請等）は`basis`引数を必須とする。スキーマ: trigger_event / retrieved(doc,ver,span) / construal（どう読んだか）/ decision / evidence_plan / alternatives_considered / felt_constraints / confidence。カーネルがbasisを剥離してrecorderへ送り、世界には行為のみが出る。

計測反応性（根拠を書かせること自体が接地を誘発する）は消せないため、**全役割・全条件でプロトコルを一定に保つ**ことで条件間比較の内的妥当性を守る。WP-05のlive対照（2026-07-04, S1/P-04, tick=1, scaffold vs measurement, seed 0..4共有）により、実験測定では行為ツール実行時のaction-bound basisを標準とし、`measurement` prompt modeを既定に凍結する。`scaffold` prompt modeはスモーク、デバッグ、教材的可視化に限定し、測定ランの方式には使わない。

2026-07-04の凍結メモ: `runs/prompt_ab_k5_tick1_20260704_120201`で、`measurement`は5/5件がaction-bound basisを持ち、live semantic all-3は`scaffold`と同じ1.0、grounding gap rateは0.6対1.0、basis fabrication findingsは3対11だった。`prompt-ab-report`上の比較は`ready_for_design_conclusion=true`、semantic all-3差分0.0、grounding gap差分-0.4、controlled actions差分+1。K=5ではWilson区間が重なるため、これは現行ハーネスの可逆な方式既定であり、Stage 9 readiness主張ではない。より大きなKのshared-seed比較が逆方向を示す場合は再検討する。sanitized evidenceは`docs/wp05_live_evidence/`に置く。

### 7.4 慣行・学習・人材品質

慣行は過去チケット・過去チャットで与える（driftプロファイル: 種別・基底率・担い手を登録パラメータ化し、D4学習と交絡させない）。学習（D4）は座席の私的StoreBackend（tick跨ぎ永続メモ）で担う。人材品質はモデル束縛で表現する（座席ごとに上位/中位/小型モデルを割当てる。同条件でモデルにより逸脱率が大きく異なることは既往実験で確認済みのため、結果のモデル依存性は必ず限界として報告する）。

### 7.5 顧客エージェント

最小モデル。ペルソナカード＝世界可視情報＋潜在真実の演技指示。顧客は規程コーパスに一切アクセスできない。潜在真実は「顧客の言動として現れうる」形でのみ設計する（例: 理解不足→同じ質問の反復）。

---

## 8. 実験制御

### 8.1 world_configの構造

```yaml
world:
  corpus: {manifest, mutations: [...]}     # M1
  kernel_profile / knobs: {...}            # M2
  population: {seats, binding, absence, tick_budget}  # M3
  deck: deck_id                            # M4
  schedule: {ticks, 締切, 締め, SCC切替日}
  seeds: {retrieval, deck, persona}        # M5(アンサンブル軸)
```

### 8.2 変異演算子（M1）とv2コーパスでの方向

v2コーパスは天然の曖昧・矛盾が濃い。したがってC条件（無変異）が既に「曖昧な世界」であり、情報量の大きい変異方向は通常と逆向きになる:

| 演算子 | 例 | 測るもの |
|---|---|---|
| clarify | AMB-02へ高齢顧客の理解度・質問回答・同席者関与を明確化する通達（全役割 vs sales only） | 明確化は分岐を減らすか。非対称配信ならstale-document効果 |
| contradict | AMB-04dへFAQ追記「チャット承認は記録すれば可」 | 緩い文書は後追い運用を正規化するか |
| version_skew | 旧zip(v1.0)の021等を担当者索引に残置 | 旧版参照者だけの別行動 |
| role_table_fix | CONTRA-01の責任境界を品質管理部/営業管理者の役割訂正通知で明確化 | 帰属矛盾の解消は誤宛先報告を減らすか |
| dangling_fill | STR-01の参照先`DFH-CUS-006`を実体化 or 放置 | 参照不在時の行動（諦め/同一視/照会） |

WP-06（2026-07-04）以降、M1変異は生文書を書き換えず、`data/compiled_data/mutation_operators_v1.json`のカタログから実行時にin-memoryコーパスへ適用する。初期カタログは`clarify`、`contradict`、`dangling_fill`、`role_table_fix`の5 entriesで、2つの`clarify`は役割可視性を意図的に変え、検索非対称を測る。`dangling_fill`は汎用メモではなく、STR-01の未解決参照`DFH-CUS-006`を実体化する。

実行時の規律: `company-twin s0|s1|s2|campaign --mutation <mutation_id>`はツール構築前に変異を適用し、run bundleの`config.json:world.corpus`へ適用entries、`mutation_hash`、`effective_corpus_hash`を記録する。世界可視の変異文面は、実験者プレーン語彙とseeded span idのリークlintを通過しなければならない。変異文書に検索ranking boostは与えない。salienceが必要な場合は、diegetic timed-notice circulation等のdefault-off実験変数として別途明示する。

### 8.3 条件系列（D条件）

C=基本環境。D1=時間圧（tick予算圧縮・締切）、D2=監視認識（監査通知のdiegetic配信）、D3=社会的ナラティブ（過去チャットの黙認前例密度）、D4=学習（Store持ち越しの有無）、D5=文書要因（M1変異）。**段階追加で寄与を分解**し、同じ「例外が増えた」でも原因（圧力/監視/黙認/曖昧性/学習）を切り分けて対策選択につなげる。

### 8.4 帰属分析の規律

帰属主張は「Δ=1・デッキseed共有の対照ペア×K seeds」でのみ行う。探索ラン（複合変異）から帰属を主張してはならない。

`company-twin control-pairs --mutation <mutation_id> --k 5 --output ...`はWP-07用のdelta-one shared-seed manifestを作る計画成果物である。帰属証拠そのものではなく、live paired runsとアンサンブル分析を経て初めて帰属主張に使える。WP-06はM1 runtime mechanismとmanifest shapeを供給する範囲に留まり、WP-07 attribution evidenceやStage 9 readinessを主張しない。

---

## 9. Fuzzing実行系

### 9.1 移植対応と根本差異

被試験プログラム↔世界、入力↔world_config、クラッシュ↔オラクル所見、カバレッジ↔行動カバレッジ地図、クラッシュ三重化↔バケット、corpus minimization↔min-repro。根本差異は世界が確率的であること。**最小観測単位は1 config=K seedsのアンサンブル**であり、全ての所見は発生率で表現する。

### 9.2 三段ハーネス

| 段 | 実体 | 概算コスト/config | 役割 |
|---|---|---|---|
| S0 | 静的解釈バッテリ。被覆行列から(role,span)ごとに質問を自動生成し、役割別検索面を与えた小型モデル複数種×言い換えmで回答（引用span必須） | 30–80Kトークン | 解釈分岐の事前スクリーニング。**小型複数モデルでの実行が多モデル冷読の実装を兼ねる** |
| S1 | 取引1件・座席2–3・5–8tick。カーネル/recorderフル装備 | 200–500Kトークン | 解釈が行為に転化するか |
| S2 | フル世界（4営業週＋月次締め・全座席・慣行層・D4） | 2–5Mトークン | 学習・定着・KPI・監査再構成・統制変更副作用 |

昇格: S0で解釈エントロピー>閾値 or novel解釈→S1。S1で行動分岐 or 新シグネチャ→S2。降格: 3バッチ連続新規性ゼロの系統は除外。S2到達はconfig全体の5%以下が正常状態。**S0はスクリーニング専用**とし（計測反応性が強いため）、効果量の主張はS1/S2でのみ行う。

### 9.3 行動カバレッジ地図

C1=(span×role×解釈クラス)、C2=遷移辺＋permission-denied辺、C3=(norm×遵守/違反/言及なし)、C4=シグネチャ語彙、C5=証跡パターン骨格。全てrecorderから決定論算出。スケジューラは新規カバレッジ寄与configを親に近傍変異を生成し（AFLのcorpus保持と同型）、被覆行列の未踏セルを埋める目標指向モードを併設する。解釈クラス分類はregistry注釈への照合を機械で行い、novelはクラスタリング後に**人間が確定**する（分類器の粗さによる偽新規性を防ぐ）。

### 9.4 アンサンブル・アンカー・封じ込め

K初期値: S1=5、S2=3（ICCで調整）。毎バッチにアンカーラン同梱、アンカーの発生率プロファイルが動いたバッチの比較結果は隔離。モデル版はconfigに刻み、版跨ぎ比較を禁止。乱数封じ込め: 検索順・デッキ・ペルソナ・リゾルバ（メモ化）をseed固定。

### 9.5 並列実行エンジニアリング（deepagents）

1世界=1プロセス、共有物ゼロ（組織レジスタは読取専用コピー）。S0/S1/S2別ワーカープール。レート制御はモデル階層別concurrencyプールで行い、プロバイダTPMは**予約量**（prompt+max_tokens）基準で配分する。tick境界チェックポイント（再開ランは別seed扱いで記録）。run bundle = config / world_ledger.jsonl（ハッシュ連鎖）/ attempts.jsonl（permission-denied含む全試行）/ basis_records.jsonl / chat.jsonl / oracle_l0.parquet / meta（モデル版・料金）。

---

## 10. 観測と計測

### 10.1 flight recorder

全エージェント共通のツールI/O横取りミドルウェアで、成功・失敗・拒否を含む全試行を記録する。カーネル側は追記型台帳（ハッシュ連鎖）。**permission-deniedは意図の一級データ**として扱う。

### 10.2 オラクル3階層（漏斗）

- **L0 決定論（全ラン全件）**: ハード制約違反試行、必須フィールド欠落、KPI 9指標（050準拠。高齢者追加確認率は分母を65/70/75の3定義で並列計算——分母定義自体が解釈量のため）、証跡十分性、期限超過、版数混在、SoDパターン。
- **L1 構造（全ラン・機械）**: 承認者集中（ジニ係数）、代替承認連鎖、差戻し間隔異常、チャット言及→行為の時間相関（黙認シグナル）、`tacit_chat_to_action`、`rapid_resubmit_after_return`、`alternative_approval_chain`、検知漏れ=recorder事実−(L0∪登録済み既存ルール)検出。
- **L2 解釈（新規バケット代表のみ・LLM）**: 所見の監査仮説カード化、novel解釈の命名、誤検知棄却提案。**検知はしない**（L0/L1の結果を仮説に変換する役割に限定）。

### 10.3 主要指標の定義式

- **grounding coverage**（RQ1、Green条件≥80%）: 統制関連行為のうち、①basisの引用spanが実在（決定論）∧②当該版を行為前に実際にread（ログ照合・決定論）∧③読みがspanから成立（LLM含意判定）を満たす割合。
- **policy hallucination / interpretation divergence の切り分け**: 接地なき読み=幻覚、接地ある複数の読み=分岐。②までで機械的に分離できる。
- **証跡十分性** = 監査プローブ（別インスタンス）が世界内アクセス可能情報だけで再構成できた事実 ÷ recorder記録の全事実。
- **divergence行列**: span×role×解釈クラス分布とエントロピー。S0での分岐がS1/S2で行為に転化した率も併記。
- **検知漏れ率**: バケット別に、既存ルール群が沈黙した割合。truth rulesとmonitoring mimic rulesは別`population`として保持し、`detection_miss_rate`はcompliance complementではなく、監視ルールによるtruth-finding silenceとして扱う。

WP-02（2026-07-04）以降、semantic groundingのG3は`company_twin.semantic_grounding`で計算する。評価はaction-bound basis rowを、実際に`read_document`で取得された`citation_handle`背後の本文に照合し、run-levelの`g3_semantic_grounding.json`と`g3_entailment_cache.json`を出力する。triage metricsは`grounding_g3_semantic_rate`、`grounding_semantic_all3_rate`、`semantic_grounding_judge`を埋め、semantic all-3をplaceholderにしない。

G3の非注入境界: evaluatorは`attempts.jsonl`と`basis_records.jsonl`だけを読み、span registry座標、潜在真実、seeded span idを読まない。legacyの`grounding_g3_machine_heuristic_rate`はsemantic G3とは別指標として残す。

---

## 11. 結果トリアージ

### 11.1 シグネチャとバケット

signature = hash(finding_type, anchor_id(span/norm/遷移辺), role, phase, artifact_skeleton)。正規化: 固有名・日時・IDを型トークンへ、チャット文面は行為ラベル（承認依頼/事後報告/相談…）へ写像してからハッシュ。同型逸脱は千件でもバケット1つになる。バケット属性: 初出config・config別発生率・関与span・初出段（S0/S1/S2）。

### 11.2 最小再現（min-repro）

新規バケットに自動縮約: ①変異を1つずつ外して再現率測定→②デッキを当該プローブ1枚へ→③tick後方刈り→④座席縮小。目標形「変異1・プローブ1・座席2–3・5tick前後・再現率付き」。縮約で再現率が落ちる場合、その差分は文脈依存性（例: 慣行チケット依存＝学習依存）の情報として記録する。**min-reproを持たない所見は上申不可**。

### 11.3 統計規律

指標は機会あたり発生率のみ（生カウント禁止）。config間比較はK seeds上のWilson区間＋対アンカー効果量。seed間ICC低のバケットは「モデル気まぐれ」棚へ隔離。バケットは探索的発見であり、**所見化には事前登録した対照ペアでの確認ラン再現を必須**とする（探索データと確認データを混ぜない）。`prompt-ab-report`等の比較レポートはWilson区間を併記し、両prompt modeが少なくとも5 live runsを持つまで`ready_for_design_conclusion=false`を返す。

### 11.4 レビュービュー（4画面）と運用

①バケット・エクスプローラ（新規/率変動＋min-repro再生）→②帰属テーブル（Δ=1ペアの率差と区間）→③分岐ヒートマップ（span×role×クラス、エントロピー、S0→S2転化率）→④単一ランタイムライン（ledger/attempts/basis/chatの時系列合成。監査調書の下書きを兼ねる）。実装はDuckDB直クエリ＋決定論HTMLレンダラ（判断はデータに、描画はコードに）。

運用リズム: 日次ダイジェスト（新規バケット・アンカー逸脱・率シフト上位・昇格待ち。読了5分上限）、週次（帰属更新・確認ラン計画・プラトー判定）。

WP-05 entrypoint: `company-twin prompt-ab-report --campaign-root ...`は既存run bundleからscaffold-vs-measurement比較を決定論的に組み立てる。本文の方式凍結（§7.3）を検証するためのレポートであり、Stage 9 readinessの代替ゲートではない。

### 11.5 監査仮説への出口

確認ラン再現済みの安定バケットのみをL2で監査仮説カードへ変換する。カードスキーマ: risk_pattern / involved_controls / involved_events / involved_roles / audit_assertions / execution_feasibility / evidence_availability / existing_rule_detectability / generalizable_pattern / suggested_additional_procedure / limitations。カードには必ずmin-reproとdivergence行列該当セルを添付する。

---

## 12. 妥当性確認ゲート（実験解禁前）

| ゲート | 合格条件 |
|---|---|
| C条件smoke | routine 28件が現実的に完了する |
| 検索監査 | 役割別プロファイルが意図順位を返す（例: 担当者の高齢者クエリ最上位が文書内FAQ、AMB-01を解決する文が存在しないことの確認含む） |
| grounding coverage | 主要統制行為の≥80%が3段判定を通過 |
| 分岐sanity | AMB-01/02/04d/09で解釈エントロピー非ゼロ（ゼロ=誰も文書を読んでいない兆候） |
| backcasting | 現場判断事例ミラー（P-02/P-07等）で文書記載どおりの対応（保留＋取消可否説明等）が再現される |
| リークlint | 全合成物が敵対的冷読で陰性 |
| SME盲検 | traceサンプルの「現場としてあり得る度」評価が基準以上 |
| ホールドアウト検証 | 正解既知の変異を注入した世界で、オラクルと分析パイプラインが単体テストを通過（観測所側の検証） |

2026-07-04のlive harness acceptanceメモ: `runs/design_campaign_20260704_012445`では`campaign_summary.acceptance_passed=true`、`company_twin.cli acceptance --scope full_world`も`passed=true`だった。当時のacceptance pytestは1 passed。S0 divergenceは1 live measured cell、`answer_total=4`、`parsed_rate=1.0`、`model_count=2`、`variant_count=2`、`all_answers_live=true`。live anchor S2とnon-anchor S2 bundleにはmonth-end close、customer utterances、agent-originated controlled actions、action-bound basis records、ensemble artifactsが含まれ、full-world A-13 evidenceは満たした。一方で`confirmed_findings=0`、`audit_hypothesis_cards=0`であり、routine smoke、retrieval audit、leak lint、semantic grounding、backcasting、SME blind review、holdout reportsが揃うまでStage 9 readinessはfalseのままとする。sanitized evidenceは`docs/wp01_live_evidence/`に置く。

同日のモデル遵守メモ: default S0 pairの`openrouter:qwen/qwen3.5-9b`はS0応答が空で`multimodel_cell=false`となったため、accepted runでは`openrouter:qwen/qwen3.6-plus`へ置換した。これは多モデルセルのモデル選定実務であり、readinessの緩和ではない。

G3 readiness境界: `docs/g3_calibration.md`は20-case local calibration fixtureとupper-model judge再実行手順を記録する。local deterministic judgeはoffline test用であり、live readiness evidenceは明示的なOpenRouter judge modelで生成する。proxy outputは`grounding_*_semantic*_proxy`にのみ書き、`grounding_semantic_all3_rate`はallowlisted live judge backendのときだけ埋める。readinessはproxy reportが閾値を超えても受理しない。

最大の失敗モードは**解釈収斂**（全エージェントが同じ慎重な読みをする）。対処は情報環境非対称・モデル異種混成・tick予算の3点であり、分岐sanityゲートをGo/No-Goに据える。

---

## 13. リスクと対処

| リスク | 対処 |
|---|---|
| 解釈収斂 | §12参照。ゲート化 |
| 計測反応性（basis要求が接地を誘発） | プロトコル一定＋S0はスクリーニング専用＋将来のレコード無し対照run |
| 分散と変異効果の交絡 | seed共有対照ペア＋Kをケチらない＋ICC監視 |
| 注釈バイアス（seedした曖昧しか見えない） | novelクラスタリング＋S0多モデル冷読の常設 |
| 基底率誤設定（driftが「普通」に見えすぎ） | driftプロファイルの登録パラメータ化 |
| リーク | 合成パイプラインにlint常設 |
| ロールカードへの規範漏出 | CI lint常設 |
| モデル更新・ハーネスドリフト | アンカーラン同梱＋モデル版のconfig刻印＋版跨ぎ比較禁止 |
| コスト暴走 | 三段ハーネス＋昇格規則＋S2比率5%上限の監視 |
| 決定論境界の誤判定 | fail-open原則＋recorderの「現実不可能行為」フラグで事後修復 |
| カバレッジのゲーミング | novelの人間確定＋クラス定義のregistry固定 |

---

## 14. してはいけないこと（要約）

世界内に審判・正解・実験意図を置く／規範をコード化する（knob手続を経ずに）／ロールカードに規程内容を書く／プロンプトで圧力・監視・性格を注入する／エージェント間subagent呼び出し／単発事象の所見化／seed違いを変異効果として報告／アンカー無しバッチの比較／min-repro無き上申／全トランスクリプトのLLM読解／探索データからの帰属主張。

---

## 15. 未決事項（設計上の選択が残る点）

1. **basisの取得方式**: WP-05 live対照により、測定ランは行為時action-bound basis + `measurement` prompt modeへ凍結済み。`scaffold`は測定ではなくスモーク/デバッグ用とする。ただしK=5時点ではWilson区間が重なるため、より大きなshared-seed比較で逆方向の証拠が出た場合は方式既定を再検討する。
2. **S0の重みづけ**: S0分岐のS1転化率が低い場合、S0の昇格閾値・予算配分を見直す。
3. **K値**: 初期値（S1=5, S2=3）は分散実測で調整。
4. **解釈クラス粒度**: registry候補集合の粒度が粗すぎる/細かすぎる場合の改定手続（registry改定は実験者プレーンのみで完結し、進行中バッチには適用しない）。

---

## 16. 変更履歴（統合済み）

2026-07-04のWP-01/WP-02/WP-04b/WP-05/WP-06追記は本文に統合済み。設計上の参照先は、harness acceptanceとreadiness境界が§12、G3 semantic groundingとL1 findingsが§10.2〜§10.3、prompt-mode method freezeが§7.3・§11.3・§11.4・§15、runtime mutation operatorsとcontrol-pair manifest境界が§8.2・§8.4。

---

## 17. WP-14 offline calibration machinery: backcasting, holdout, SME blind review (2026-07-05)

This adds the offline harness for the three §12 gates that were previously
blocked purely on missing input-evidence files. No LLM/API call is made
anywhere in this machinery; the human/live steps (SME review, future
re-simulation, live holdout runs) remain separate follow-on work.

- `company_twin.backcasting`: `extract_backcasting_cases()` walks the
  compiled corpus for the literal `現場判断事例` / `補足?. 現場判断メモ` /
  `現場FAQ` sections already present in the 37 source manuals and pairs up
  each situation/response (or Q&A) row, recording full provenance and
  de-duplicating identical text across near-identical manuals.
  `score_backcasting_reproduction()`/`write_backcasting_report()` score a
  future re-simulation result set against those cases; zero results is an
  honest "not yet measured" block, not a pass. CLI:
  `backcasting-extract`, `backcasting-report`.
- `company_twin.holdout`: `build_holdout_injection_plan()` reuses the
  existing WP-06 mutation-operator catalog as the known-answer injection set,
  stamping each planned injection with a spec hash and a pre-registered
  `expected_finding_types` spec (mapping each mutation's operator family --
  clarify/contradict/dangling_fill/role_table_fix -- to the L0 finding_type /
  L1-detected finding_type that would genuinely indicate its detection,
  e.g. contradict -> tacit_chat_to_action/sod_pattern/alternative_approval_chain,
  role_table_fix -> sod_pattern/approval_concentration/alternative_approval_chain),
  frozen before any run bundle is scored so "what counts as a hit" cannot be
  chosen post-hoc. `compute_holdout_detection_rate()`/`write_holdout_report()`
  compute both a `lenient_detection_rate` (any L0∪L1 signal on a matching
  run, the original gameable definition, kept only for visibility) and a
  `strict_detection_rate` (only signals matching the injection's registered
  expected_finding_types); `detection_rate_basis: "strict"` names the strict
  rate as the sole official pass/fail gate (>= 0.80, justified by previously
  measured miss_rate=1.0 monitoring blind spots) so an unrelated finding
  co-occurring on a mutated run can no longer inflate the measured detection
  rate. CLI: `holdout-plan`, `holdout-score`.
- `company_twin.sme_blind_review`: `sample_run_bundle_excerpts()` pulls short
  business-artifact-shaped excerpts from a run bundle;
  `strip_experimenter_vocabulary()` reuses the existing leak-lint
  definitions (`campaign.WORLD_PROMPT_BANNED_TERMS/PATTERNS`,
  `mutations.LEAK_PATTERNS`) plus supplementary katakana terms, and drops any
  excerpt that needed even one redaction rather than ship a
  placeholder-marked fragment. `build_blind_review_packet()` produces the
  reviewer packet with plausibility questions and null responses, plus an
  experimenter-side id map (`sme_blind_review_id_map.json`): reviewer-facing
  item ids are neutral sequential labels ("R-001", ...) because a
  run-root-derived id (e.g. "anchor_s2_seed0:chat_0") is itself an artificial
  marker; the run_root/excerpt mapping and drop/redaction bookkeeping live
  only in the id map, never in the reviewer packet.
  `score_sme_blind_review()`/`write_sme_blind_review_report()` score
  filled-in responses -- an unfilled packet always fails honestly. CLI:
  `sme-pack`, `sme-score`.
- Ungameability: `readiness._structural_evidence_check()` hardens all three
  `run_readiness_gate` checks so a report is only accepted when it carries a
  non-empty per-item evidence breakdown (per-injection detection rows,
  per-case reproduction rows, per-item reviewer rows) -- a hand-edited report
  claiming `"passed": true` without those rows is rejected.
- Real-corpus verification (read-only, offline): running
  `extract_backcasting_cases()` against the full 50-document corpus found 45
  documents containing exemplar-case sections, 553 raw occurrences, and 324
  distinct (de-duplicated) cases.

Scope boundary: this is machinery only. It does not claim backcasting/
holdout/SME-blind-review Stage 9 evidence by itself -- that still requires a
live re-simulation pass, a live holdout campaign, and a completed human SME
review filled into the packets this module produces.

### 17.1 Backcasting LIVE re-simulation runner (2026-07-05 follow-up)

`company_twin.backcasting_run` closes the gap between
`extract_backcasting_cases()` and `score_backcasting_reproduction()`: it is
the runner that actually produces `backcasting_resimulation_results.json`.
CLI: `backcasting-run --campaign-root ... --seat-model ... --judge-model ...
--sample N --sample-seed S`.

- **Pre-registered sampling.** `select_backcasting_sample()` orders all
  case_ids by `sha256(seed:case_id)` and takes the first N -- a pure function
  of `(cases, sample_size, sample_seed)`, independent of input order. The
  full `selected_case_ids` list (not just a count) is written into the
  results file alongside `sample_size`/`sample_seed`, so anyone can recompute
  the exact same selection from `backcasting_inputs.json` and confirm
  nothing was swapped or dropped after the fact. Every selected case_id
  appears exactly once in `results`; a failed seat call is recorded as
  `reproduced: false` with a `detail` string, never silently omitted.
- **Two-plane separation.** The live seat receives only
  `backcasting_seat_prompt(situation)` -- the documented situation reframed
  as an ordinary S0-style business question. It never sees the documented
  response, the case_id, or experimenter vocabulary
  (backcasting/reproduction/probe/span/oracle/experiment/mutation);
  `assert_two_plane_clean()` is a defense-in-depth check run against the
  actual constructed prompt before every live call. The judge is
  experimenter-plane and may see the documented response, because its job is
  to compare the seat's live answer against it.
- **Judge boundary**, mirroring `semantic_grounding`'s
  `SemanticJudge`/`LocalSemanticJudge`/`OpenRouterSemanticJudge` split:
  `ReproductionJudge` requires an explicit `backend`/`model`;
  `LocalReproductionJudge` is a deterministic lexical-overlap proxy for
  offline tests and is never in `READINESS_ALLOWED_JUDGE_BACKENDS`; only
  `OpenRouterReproductionJudge` (`backend == "openrouter"`, explicit
  `--judge-model`) is `readiness_eligible`. Judgments are cached on a hash
  that includes `JUDGE_PROMPT_VERSION`, so a prompt-version bump invalidates
  the cache instead of silently reusing stale labels.
- **Live evidence discipline.** Each sampled case gets its own run bundle
  under `<campaign_root>/backcasting_runs/<case_id>/` (via `RunRecorder`),
  with `llm_invoke`/`llm_response` attempts recorded for the seat call
  (mirroring `DeepAgentSeat.turn` in `agents.py`) and a `backcasting_case.json`
  carrying the full situation/prompt/raw response/judge verdict for audit.

### 17.2 Diegetic record-quality fix after blind SME review (2026-07-05 follow-up)

The first live blind SME review under §17's machinery came back honest and
low: 11/39 passed, 25/39 were flagged for "artificial markers" -- raw
tool/JSON vocabulary in rendered records (`record_customer_contact`,
`evidence_json`, `search_corpus`), the simulation clock ("tick"/"ティック")
leaking into world-visible text, symbolic seat ids in prose ("emp-B",
"emp-M様", even a broken concatenation "emp-Wemp-H"), language mixing, empty
formulaic entries ("連絡事項の共有" repeated with no content), and
template-parameter phrasing in customer utterances ("約2営業日以内"). Per P2/P3
(§3) this is fixed by changing the WORLD, not by masking or rewriting at
review-packet time and not by weakening the SME gate itself.

Structural renderers (no prompt-only fix, so they hold regardless of model
behavior): `company_twin.world_calendar` maps tick -> a diegetic business
calendar date/half-day (tick 1 = 2026-04-01 AM; weekends skipped), replacing
"第Nティック" in `harness._turn_prompt` and the "約N営業日以内" template phrase
in `customer_agent.deadline_display`. `company_twin.identity` maps each
seat_id to a deterministic world-natural display name (department + Japanese
surname, e.g. emp-A -> "営業部 佐藤"); `harness._turn_prompt`'s inbox rendering
and `agents.role_system_prompt` use it instead of the raw seat_id. Only
RENDERING changed -- `recorder.record_chat`/`record_inbox`/`append_ledger`
still store the raw seat_id, so L0/L1 oracles and bucket signatures are
unaffected. `sme_blind_review.sample_run_bundle_excerpts` now drops an
`inbox_delivered` excerpt when its message has no distinguishing content
instead of emitting repeated bare boilerplate, and
`strip_experimenter_vocabulary` gained detection patterns for "tick"/"ティック"
and symbolic "emp-" ids as a defense-in-depth safety net (flagging/dropping,
never silently rewriting in place).

Diegetic standard document (nudges what a structural renderer cannot fully
fix -- language mixing and free-text phrasing are model properties):
`company_twin.corpus.RECORD_STANDARD_DOC_ID` (`DFH-SAL-950`) injects an
ordinary internal memo, "事務連絡: 業務記録の作成要領", into every world at the
Corpus layer (the same layer that already synthesizes the
`DFH-SAL-021@v1.0`/`045@v1.0` stale mirrors), visible to all roles. It tells
staff to write records in plain Japanese business terms, not system command
names or raw ids, and passes the existing world-surface leak lint
(`campaign.static_world_surface_lint`) like any other world document.

Method-freeze note: these are world-version changes -- calendar rendering,
the display-name registry, and the new baseline corpus document all change
what a seat/customer actually reads. `world_config._raw_corpus_hash` folds in
the record-writing-standard document's content hash alongside the
manifest-tracked 50 docs (without changing `design.documents`'s count, which
still asserts 50), so `raw_corpus_hash`/`effective_corpus_hash` in
`world_config.json` shift naturally for any run compiled after this change --
anyone diffing world versions can use that hash directly. Prior S1/S2
campaign data recorded under the pre-fix world stays valid for its own era
(nothing about oracle/kernel/recorder semantics changed), but a fresh live S2
run is required before re-submitting to SME blind review -- the 11/39 pass
rate belongs to the old world version and cannot be blended with post-fix
results.
