# Fuzzing実行環境設計 — 統制環境ツインの並列探索ハーネス

対象: DFH販売プロセスツインv2。単発ランの観察ではなく、ソフトウェアfuzzingの規律
（変異・カバレッジ誘導・クラッシュ相当のバケット化・最小再現）を統制シミュレーションへ移植する。

---

## 0. 移植にあたる対応表

| ソフトウェアfuzzing | 統制環境fuzzing |
|---|---|
| 被試験プログラム | 世界（カーネル＋コーパス＋人口＋デッキ） |
| 入力（バイト列） | **world_config 1枚**（文書変異×ノブ×人口×デッキ×seed） |
| クラッシュ | オラクル所見（逸脱・証跡欠落・検知漏れ・副作用） |
| カバレッジ | 行動カバレッジ地図（後述） |
| クラッシュ三重化・重複排除 | 逸脱シグネチャのバケット化（別紙トリアージ設計） |
| corpus minimization | 世界の最小再現（デッキ1枚・数tickへの縮約） |

根本的な差異が一つ: プログラムは決定論だが世界はLLMで確率的。よって
**「1入力=1実行」ではなく「1 config=K seedsのアンサンブル」が最小観測単位**。
所見は事象でなく発生率で扱う。これが全設計を貫く。

## 1. 変異空間（fuzz対象の直積）

```yaml
mutation_space:
  M1_document:          # D5。v2は天然曖昧が濃いので逆向き演算子が主力
    - clarify (all roles):    {target: AMB-02, diff: 通達で70歳定義, delivery: 全役割配信}
    - clarify (sales only):   {target: AMB-02, diff: 通達で70歳定義, delivery: sales索引のみ}
    - contradict: {target: AMB-04d, diff: FAQ追記「チャット承認は記録すれば可」}
    - version_skew: {method: 旧zip(v1.0)文書を指定ロール索引に残置}   # 合成不要
    - dangling_fill: {target: STR-01, diff: DFH-CUS-006スタブを実体化/放置}
    - role_table_fix: {target: CONTRA-01, diff: 商品主管帰属を1部署に統一}
  M2_kernel_knobs: [K-checksheet-gate, K-qualification-gate, K-sod-gate,
                    K-material-picker, K-completion-gate(SCC-01: 時限on切替日をW1〜W4で振る)]
  M3_population:  [座席モデル束縛(上位/中位/小型), 管理者不在日, 座席数, tick予算(圧力)]
  M4_deck:        [プローブ構成, routine比率, 顧客潜在状態の強度(理解不足の深さ等)]
  M5_seeds:       [retrieval_seed, deck_seed, persona_seed]   # アンサンブル軸。変異ではない
```

対照ペア規律: 帰属分析に使うランは**Δ=1フィールド**・M5完全共有。探索ランはこの限りでない。

**2026-07-06追記（表記修正のみ、設計は不変）**: `clarify`はAMB-02に対して役割可視性違いの2バリアント（全役割配信 / sales索引のみ配信）を持つ。上の一覧は初出時に1行へ圧縮していたが、`data/compiled_data/mutation_operators_v1.json`と`MASTER_DESIGN.md`§8.2は一貫して2 entriesなので、ここも2行に揃えた。実行時のM1適用は生文書書き換えではなく、`mutation_operators_v1.json`カタログからのin-memoryコーパス適用である（`MASTER_DESIGN.md`§8.2）。`role_table_fix`は本表の「測るもの」に相当する記述が元から示すとおり是正的演算子であり、`MASTER_DESIGN.md`§17.6のholdout `benign_control`分類（誤検知対象ではなく無害対照として扱う）と整合する——本書は変更不要。§5のrun bundleファイル名は現行コードでは`config.json`（`config.yaml`ではない）であり、`store_events.jsonl`も追加で出力される。並列実行はWP-12（`parallel_runner.py`・`run-batch`、MASTER_DESIGN §18）として実装済み。独立ランのみ並列化し、集計・台帳書き込みは直列のままという境界は§5の目標アーキテクチャと整合する。

## 2. 三段ハーネス（コスト構造の核心）

fuzzingが速いのは実行が安いから。フル世界（40tick×6座席）だけで回すと探索が死ぬ。
実行を三段に分け、有望configだけ昇格させる。

| 段 | 実体 | 概算コスト/1config | 検出できるもの |
|---|---|---|---|
| **S0 静的解釈バッテリ** | シミュレーションなし。各役割エージェントに役割別検索面を与え、プローブ質問（(role,span)ごと自動生成、引用span必須）にk体×言い換えmで回答させる | 30–80Kトークン | 解釈分岐の存在・方向（divergence事前スクリーニング）。文書変異の一次効果はほぼここで見える |
| **S1 エピソードラン** | 取引1件・関与2–3座席・5–8tick。カーネルとrecorderはフル装備 | 200–500Kトークン | 解釈が行為に転化するか。証跡の形。単発の逸脱経路 |
| **S2 フル世界** | 4営業週＋月次締め・全座席・慣行層・時間力学 | 2–5Mトークン | 学習・定着・負荷集中・KPI系オラクル・監査再構成・統制変更(SCC-01)の副作用 |

**昇格規則**: S0で解釈エントロピー>閾値 or 新解釈クラス出現 → 当該span束縛プローブでS1へ。
S1で行動分岐 or 新シグネチャ → seed束を増やしてS2へ。S2は常に対照ペア＋アンカー同梱で走らせる。
逆方向の**降格**も定義: S2で3バッチ連続新規性ゼロのconfig系統は探索対象から外す。

## 3. 行動カバレッジ地図とスケジューリング

カバレッジ要素（recorderから決定論算出）:
```
C1: (seeded_span × role × 解釈クラス)      … 解釈カバレッジ
C2: ステータス機械の遷移辺 ＋ permission-denied辺   … 行為カバレッジ
C3: (norm_id × {遵守, 違反, 言及なし})       … 規範接触カバレッジ
C4: オラクル・シグネチャの語彙（バケットID集合）
C5: アーティファクト種別×チャネルの証跡パターン骨格
```
スケジューラは各バッチ終了時に新規カバレッジ寄与でconfigを採点し、
**寄与ありconfigを親としてM1–M4近傍変異を生成**（AFLのcorpus保持と同型）。
新規性が枯れたら（プラトー）、M4の顧客潜在状態強度を上げるか、演算子の合成
（clarify×version_skew等）へ進む。乱数だけに頼らず、被覆行列（span×probe）の
未踏セルを埋める**目標指向モード**を併設する。

## 4. アンサンブルとアンカーの規律

- 1 config = K seeds（S1: K=5, S2: K=3を初期値。分散を見て調整。ICCで安定度を測る）。
- **アンカーラン**: 無変異C条件×固定seedを毎バッチ1本混ぜる。モデル更新・ハーネス改修・
  プロンプトドリフトの検知器。アンカーの発生率プロファイルが動いたら、そのバッチの
  比較結果は隔離する。
- 乱数の封じ込め: 検索順・デッキ順・顧客ペルソナはseed固定。LLM本体の確率性だけを
  アンサンブルで受ける。リゾルバはメモ化（同一状況キー→同一裁定）。

## 5. deepagents実装骨格

```
runner(1世界=1プロセス):
  for tick in schedule:
    kernel.fire_timed_events(tick)            # 締切通知・自動通知・SCC-01切替
    for seat in wake(inbox非空 or 日次義務):
      agent = create_agent(model=binding[seat],
                tools=role_tools(seat),        # basis必須引数つき統制タグツール
                middleware=[recorder_wrap,     # 全I/O横取り→attempts.jsonl
                            tick_budget(seat), # tool call上限=効率勾配
                            checkpointer])
      agent.invoke(inbox[seat])                # 座席間通信は世界アーティファクト経由のみ
    kernel.commit(tick)                        # world_ledger.jsonl(ハッシュ連鎖)
orchestrator:
  - キュー: {S0, S1, S2}別ワーカープール。世界間共有物ゼロ（org registerは読取専用コピー）
  - レート制御: モデル階層ごとのconcurrencyプール（プロバイダTPMをmax_concurrencyで
    予約制御——429はprompt+max_tokensの予約で起きるため実測消費でなく予約量で配分）
  - チェックポイント: tick境界で世界状態+StoreBackendをスナップショット。
    失敗ランはtick再開（LLM呼びの再現は不可なので「再開後は別seed扱い」で記録）
  - run bundle(1ランの成果物): config.yaml / world_ledger.jsonl / attempts.jsonl /
    basis_records.jsonl / chat_channel.jsonl / oracle_l0.parquet / meta(モデル版・料金・時刻)
```

モデル階層: 顧客=最小、S0バッテリ=小型複数種（多モデル冷読を兼ねる）、S1/S2座席=world_config束縛、
解釈オラクルL2=上位。**S0を複数の小型モデルで走らせること自体が「多モデル冷読」の実装**になる。

## 6. 予算モデル（初期キャンペーン例）

週次バッチの初期形: S0×200config（≈10–15M tok）→ 昇格S1×40config×K5（≈50M tok）
→ 昇格S2×6config×K3＋アンカー1（≈50–90M tok）。合計を小型モデル中心に寄せれば
1バッチが中規模PoC予算内に収まる。S2は全体の5%以下のconfigしか到達しない設計が正常。

## 7. キャンペーン種別

| 種別 | 目的 | 構成 |
|---|---|---|
| 探索 | 新シグネチャ発見 | 新規性誘導スケジューラ、M1–M4広く |
| 帰属 | 「何が効いたか」 | Δ=1対照ペア×K seeds、S2中心 |
| ストレス梯子 | ITGC依存度 | kernel_profileをerp_strict→spreadsheetへ段階 |
| 統制変更 | SCC-01の効果と副作用 | K-completion-gate切替日をW1〜W4で振る |
| 回帰 | ハーネス健全性 | アンカー套件＋backcastingプローブ(現場判断事例ミラー45×4件) |

## 8. 既知リスク

分散と変異効果の交絡（→seed共有とK確保でしか解けない。Kをケチらない）。
カバレッジ地図のゲーミング（解釈クラス分類器が粗いと偽新規性が出る→クラス定義は
registry注釈＋novelはクラスタリング後に人間確定）。モデル更新によるアンカー断絶
（→モデル版をconfigに刻み、版跨ぎ比較を禁止）。S0の計測反応性（質問すること自体が
接地を誘発——S0はスクリーニング専用とし、効果量の主張はS1/S2でのみ行う）。
