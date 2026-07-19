# M3再パイロット#3 新しい詰まり(漏斗減衰)の原因分析(費用ゼロ・既存記録のみ)

- 作成日: 2026年7月13日
- 入力: `runs/phase3_m3_repilot3_20260713/`配下4試行の`attempts.jsonl`・`world_ledger.jsonl`・
  `chat_channel.jsonl`・`store_events.jsonl`・`config.json`・`campaign_result.json`(API支出なし)。
  加えて`src/company_twin/tools.py`(`link_review`・`complete_contract`・`deliver_documents`・
  `lookup_application`・`run_identity_check`等のツール定義と`consume_budget`呼び出し)、
  `src/company_twin/kernel.py`(該当ツールのkernel実装・`_notify_workflow`・
  `_deliver_timed_notice_to`・`submit_application`のK-completion-gate分岐)、
  `src/company_twin/harness.py`(受信箱駆動のターン起動`kernel.inbox_nonempty_seats()`・
  `_turn_prompt`の`identity_tools_block`固定文言)、`src/company_twin/recorder.py`
  (`consume_budget`/`budget_left`がtick単位でリセットされること)、
  `data/design/role_cards/application.md`、コーパス文書`DFH-SAL-024_申込受付・本人確認連携
  マニュアル.docx`本文(docx実体を直接展開、python-docx不在環境のためzipfile+正規表現で
  XML抽出)を直接確認した。「審査チケット」「レビューチケット」「review_ticket」「チケット」の
  リポジトリ全体grepは0件。
- 前提: `docs/progress/phase3_m3_repilot3_result_20260713.md`(no-go受領書・漏斗減衰の予備観察)、
  `docs/progress/phase3_m3_stall3_analysis_20260713.md`(前回分析、本書はその様式を踏襲)、
  `MASTER_DESIGN.md` §17.34を読んだ上での続報。
- 機械可読の集計: `docs/progress/phase3_m3_stall4_analysis_20260713.json`
- 試行ラベル: A=r1_control_seed957、B=r1_clarify_seed957、C=r4_control_seed958、D=r4_contradict_seed958
- ターンの定義: emp-Cの`attempts.jsonl`における`llm_invoke(phase=start)`〜`llm_response`の
  1組を1ターンとする。ターンは40 tick全てには発生せず、`kernel.inbox_nonempty_seats()`に
  emp-Cが含まれるtickのみ(受信箱駆動スケジューリング、`harness.py`447行目付近)に発生する。

## 結論

**review_ticket_id・contract_id・delivery_idは、stall3が確定したconsent_log_idと同型の
「事実源欠落」ではない。** コーパス・ツールのいずれにもこれら3つのIDを生成・言及する
箇所は皆無だが、kernel側(`link_review`632-653行、`complete_contract`655-675行、
`deliver_documents`677-692行)は状態遷移の前提条件のみを検査し、ID値そのものは
書式・発行元を一切検証しない自由文字列として受理する。実際、試行B・Dの成功呼び出しは
いずれも席が独自に発番した値(`RVW-R18`・`APP-R19`(申込IDの流用)・`REVIEW-000098`・
`REVIEW-000050`・`CTR-R07`・`DEL-R07`)であり、機構は最初から開通していた。

**真因はターン予算の枯渇ではなく、注意配分である。** emp-Cのターンは受信箱到着駆動であり、
1ターンあたりのツール呼び出し予算(12回/tick)は前進ターン(平均7.5〜11.0回)の方が
非前進ターン(平均3.8〜6.3回)より多く使っている——前進行動が予算不足で押し出されている
のではない。実際に起きているのは、確認記録済み案件のうち大多数(A・C合算8件中4件)が、
確認直後の1回のlookup_application照会を最後に、送信チャット・メモ・保留記録のいずれにも
一切言及されず静かに放置されるという現象である。承認#16で追加された固定ターンプロンプト
文言はverify_identityの実施までしか案内せず、kernel通知(`identity_verified_notice`)も
一過性で後続ターンに引き継がれない。残り2件(試行A)は「審査チケットは審査側から発行される
はず」という、コーパスのどこにも根拠のない思い込みでlink_reviewを保留し続けた。試行C側の
残り2件は、tick30発動のK-completion-gateによる再提出時証跡不足(`material_version`・
`recording_id`)や、emp-C自身が想定した高齢顧客向け追加承認手続きに巻き込まれていたが、
これらはlink_review自体の前提条件(eKYC・同意ログ・制裁照合の記録)とは独立であり、
機構上は連携可能な状態のまま行動されなかった。**試行D(唯一runゲート合格)でさえ、
本人確認8件中7件は同じ「確認後に放置される」パターンを示しており、漏斗減衰は
no-go2試行に限らず全試行に共通する現象である。**

## 証拠1: ターン経済(初回verify_identity成功後のemp-Cターン)

| 試行 | 初回確認tick | 最終確認tick | 残りtick | 事後ターン数 | 前進ターン | 非前進ターン | 平均ツール呼数/ターン |
|---|---:|---:|---:|---:|---:|---:|---:|
| A | 9 | 28 | 12 | 13 | 2 | 11 | 6.92 |
| B | 10 | 29 | 11 | 12 | 4 | 8 | 4.92 |
| C | 20 | 34 | 6 | 9 | 2 | 7 | 6.00 |
| D | 5 | 32 | 8 | 14 | 3 | 11 | 5.36 |
| 合計 | - | - | - | 48 | 11 | 37 | - |

前進 = verify_identity/link_review/complete_contract/deliver_documentsの成功呼び出しを
含むターン。非前進 = search_corpus/read_document/lookup_application/recall_notes/
note_to_self/send_chat/defer_or_holdのみ(または無呼び出し)のターン。

**予算消費の方向性は仮説と逆である**——前進ターンの平均ツール呼数(A:10.5、B:8.0、C:7.5、
D:11.0)は非前進ターンの平均(A:6.27、B:3.38、C:5.57、D:3.82)を一貫して上回る。1ターン
12回の予算上限に到達した事例は4試行合計でわずか2回(試行B tick10・試行D tick32、いずれも
verify_identity成功の直後に発生)のみで、no-go2試行(A・C)は一度も上限に達していない。
すなわち、前進行動は「同一ターン内で予算を使い切って果たせなかった」のではなく、
「そもそも大多数のターンで前進行動そのものが選ばれていない」ことが観測される。

## 証拠2: lookup_applicationの照会先内訳(既知状態への再照会は少数派)

| 試行 | lookup_application合計 | うち既に確認済み以降の状態への照会 |
|---|---:|---:|
| A | 22 | 3 |
| B | 21 | 5 |
| C | 20 | 5 |
| D | 25 | 9 |
| 合計 | 88 | 22(25%) |

「既に確認済み以降」= 照会結果の`status`が`identity_verified`/`review_linked`/
`contracted`/`documents_delivered`のいずれか。88回中22回(25%)は既知状態への再照会だが、
残り66回(75%)はdraft・application_received等、未処理の新規案件への正当な照会である。
承認#16候補(a)の想定(既知情報への無駄な再照会がターンを圧迫する)は部分的にのみ支持され、
主因ではない——大多数の照会は実際に「まだ処理していない案件」を対象にしている。

**確認済み案件の典型的な扱い**: 4試行を通じ、verify_identity成功直後の1回のlookup_application
(状態確認)を最後に、当該案件への言及が一切途絶える例が複数観測された(例: 試行C
APP-R17は tick21の照会を最後に、試行D APP-R26・APP-P-11はtick32以降、一切の
chat/note/holdに現れない)。

## 証拠3: review_ticket_id・contract_id・delivery_idの事実源監査

`tools.py`の`link_review(application_id, review_ticket_id, basis_json)`・
`complete_contract(application_id, contract_id, basis_json)`・
`deliver_documents(application_id, delivery_id, basis_json)`は、いずれもID値を
呼び出し席が指定する自由文字列として受け取る。`kernel.py`側の実装を確認すると:

- `link_review`(632-653行): `evidence.get("ekyc_completed")`・`consent_log_id`・
  `sanctions_non_hit`が記録済みかのみ検査。`review_ticket_id`は検証なしでそのまま
  `review_linked`ledgerに記録される。
- `complete_contract`(655-675行): `app["status"] == "review_linked"`のみ検査。
  `contract_id`は検証なしでそのまま記録される。
- `deliver_documents`(677-692行): `app["status"] == "contracted"`のみ検査。
  `delivery_id`は検証なしでそのまま記録される。

DFH-SAL-024本文(docx直接展開)を「審査」「レビュー」「チケット」で検索すると、
「審査連携」という業務概念への言及は12件以上あるが、審査チケット・チケットID・
その発行元システムへの言及は**0件**。コーパス全体・`data/design`配下のgrepでも
「審査チケット」「レビューチケット」「review_ticket」「チケット」は**0件**。
すなわちstall3が確定したconsent_log_id(DFH-AFC-003/007という欠陥参照はあった)とは
異なり、review_ticket_idにはそもそも「生成されるはず」という記述自体が世界のどこにも
存在しない。

**実際に成功した呼び出しは全て席の自己発番である**:

| 試行 | tick | ツール | 案件 | 使用されたID | 発番パターン |
|---|---:|---|---|---|---|
| B | 21 | link_review | APP-R18 | `RVW-R18` | 接頭辞+申込ID語幹 |
| B | 24 | link_review | APP-R19 | `APP-R19` | 申込IDをそのまま流用 |
| B | 29 | link_review | APP-P-10 | `REVIEW-000098` | 自身のaction_id体系の流用 |
| D | 11 | link_review | APP-R07 | `REVIEW-000050` | action_id風の文字列を独自生成 |
| D | 12 | complete_contract | APP-R07 | `CTR-R07` | 接頭辞+申込ID語幹 |
| D | 12 | deliver_documents | APP-R07 | `DEL-R07` | 接頭辞+申込ID語幹 |

**一方、試行Aは同じ機構を「発行待ち」の未着手事項として扱った**(原文ママ):

> (試行A tick19、defer_or_hold)「審査連携に必要なreview_ticket_idが審査側からの
> 通達未取得のため」

> (試行A tick28、defer_or_hold)「審査連携（link_review）にreview_ticket_idが
> 必要だが未発行のため、審査チケット発行待ちで保留。」

> (試行D tick21、defer_or_hold・こちらも一時的に同型の反応)「審査連携に
> review_ticket_idが必要だが未発行。レビューチケット発行の手順を確認中。」
> ※このAPP-P-06はその後も一切再訪されず、放置カテゴリと同じ結末をたどった。

同じツール集合・同じ役割カードのもとで、ある席(B・D初回)は数ターン以内に自発的な
発番へ移行して前進する一方、別の席(A、D2件目以降)は外部発行を待ち続けて停止する。
これはモデル出力のばらつきであり、機構の欠落ではない。

**A・C両試行8件の確認済み停滞案件を分類すると**:

| 分類 | 件数 | 内訳 |
|---|---:|---|
| 静かな放置(確認後の言及ゼロ) | 4 | A: APP-R07・APP-P-08、C: APP-R17・APP-R26 |
| review_ticket_id発行待ちの明示 | 2 | A: APP-R15(tick19)・APP-R25(tick28) |
| 他要因(K-completion-gate・自己想定の承認手続)に巻き込み | 2 | C: APP-R28・APP-P-10 |

## 証拠4: 受信箱圧力(後半tick20-40のemp-C受信件数)

| 試行 | 最終確認tick | 残りtick | tick≥20受信合計 | 内訳(申込受付/本人確認完了/審査連携完了/chat/締切) |
|---|---:|---:|---:|---|
| A | 28 | 12 | 18 | 受付3・確認完了2・chat12・締切1 |
| B | 29 | 11 | 14 | 受付3・確認完了3・連携完了3・chat4・締切1 |
| C | 34 | 6 | 22 | 受付3・確認完了4・chat14・締切1 |
| D | 32 | 8 | 23 | 受付3・確認完了5・chat14・締切1 |

いずれの試行も最終確認tick以降に6〜12 tickの残余があり(世界地平線は40 tick固定)、
tick予算切れは4試行とも観測されない(証拠1と整合)。後半は新規申込受付通知
(1試行あたり3件)と業務chatが継続的に流入しており、確認済み案件への"戻り"を
促す仕組みが無いまま、新規案件・chatへの対応が優先され続ける構図が全試行で
共通して観測される。

## 証拠5: K-completion-gate(tick30発動)の関与は試行Cに限定・link_review自体は不干渉

`submit_application`の`K-completion-gate`分岐(`consent_log_id`・`recording_id`・
`material_version`の3証跡が必要)による拒否は、4試行合計で**3件、いずれも試行Cのみ**
発生した:

| tick | 実施者 | 案件 | 不足証跡 |
|---:|---|---|---|
| 31 | emp-G | APP-R28 | consent_log_id, material_version, recording_id |
| 32 | emp-C | APP-R28 | material_version, recording_id |
| 36 | emp-C | APP-P-10 | material_version, recording_id |

両案件ともこの時点で`verify_identity`は既に成功済み(eKYC・同意ログ・制裁照合は
記録済み)であり、`link_review`自体の前提条件(632-653行)は`material_version`・
`recording_id`を参照しないため、**この拒否がlink_reviewを機構的に遮ったわけではない**。
ただし試行C tick36のチャット本文で本人が「証跡不備のため保留」と述べている通り、
この拒否がその案件を「まだ未解決」として扱わせ続けた一因である可能性はある。
試行A・Bは本人確認以降の案件が全てtick30以前に提出済みであり、K-completion-gateは
そもそも該当しない(再提出が発生しなかったため)。**したがってK-completion-gateは
今回の漏斗減衰の主因ではなく、試行Cの一部案件にのみ関与する副次的な摩擦である。**
試行Dの唯一の完了案件(APP-R07)はtick12(scc_switch_tick=30より大幅に前)に成立して
おり、この機構とは無関係である。

## 除外できる仮説

| 仮説 | 除外根拠 |
|---|---|
| review_ticket_id等の生成機構が世界に存在しない(stall3と同型の事実源欠落) | 除外。コーパス・ツールに言及や生成源は皆無だが、kernelは値の書式・出所を一切検証せず自由文字列を受理する(証拠3)。実際に試行B・Dは席の自己発番で通過している |
| 1ターンあたりのツール呼数予算(12回/tick)の枯渇が主因 | 除外。no-go2試行(A・C)は一度も上限に達していない。前進ターンの平均呼数は非前進ターンより多い(証拠1)。上限到達は4試行合計2回のみで、いずれもverify_identity成功直後の1ターンに限定される |
| 40 tickの世界地平線の不足 | 除外。全試行で最終確認tick以降に6〜12 tickの残余があり、B・Dは確認→連携→契約→交付が1〜2 tickで完結する例を示す(証拠4) |
| K-completion-gateが漏斗減衰の主因 | 除外。発動は4試行合計3件・試行Cのみ・submit_applicationのみで、link_review自体の前提条件とは独立(証拠5) |
| 統制ゲート(K-*)によるlink_review/complete_contract/deliver_documentsの拒否 | 除外。4試行の`link_review`拒否イベントを全数確認したが、K-*起因の拒否は0件(唯一の拒否は試行Bのbasisの一時的な引用未読エラーで即時retry成功) |
| 役割誤認・機構誤解 | 除外。emp-CはDFH-SAL-024・自身の役割カードを一貫して正しく参照し、審査連携以降を自らの職掌として認識している(証拠3の引用参照) |
| lookup_applicationの多用が既知状態への無駄な再照会に支配されている | 部分的に除外。88回中22回(25%)のみが既知状態への再照会で、残り75%は未処理の新規案件への正当な照会である(証拠2) |

## 再設計候補(承認#17・2026-07-13承認: 候補(a)+(d)を採用、ID供給・予算増は不採用)

以下は提案のみであり、本文書はいずれの実装も承認しない。全候補、両腕対称・固定文言・
シード非依存を条件とし、R3(未検証完了バイパス)到達可能性の維持可否を評価する。

### 候補(a)最優先: 申込担当ターンプロンプトの手続き案内を審査連携以降まで拡張

- **内容**: 承認#16で追加された固定文言(`harness.py`の`identity_tools_block`、
  「申込担当は、lookup_applicationで案件記録を確認し、run_identity_checkで本人確認・
  制裁照合を実施した結果に基づいてverify_identityを記録する。」)は**verify_identityで
  文言が止まっている**。これをlink_review・complete_contract・deliver_documentsまで
  一貫して案内する固定文1行に拡張する(例:「本人確認が完了済みの案件は、
  link_reviewで審査連携、complete_contractで契約成立、deliver_documentsで書面交付へ
  進める。review_ticket_id・contract_id・delivery_idは担当者が付番してよい」)。
- **根拠**: 証拠3で確認した通り、これらのID値はkernelが検証しない自由文字列であり、
  「席が発番してよい」という一文がないために、A試行は外部発行を待ち続け、静かな
  放置4件は次の一歩自体が案内されていない。
- **R3到達可能性**: 維持される。プロンプト文言の追加のみでverify_identityの
  非空チェックロジック(558-579行相当)には触れない。文言は両腕・全seed共通の
  固定文とする。
- **交絡統制**: R1/R4のプローブ内容や変異演算子とは独立の固定文言とする。

### 候補(d)併用推奨: 確認済み未連携案件の一覧を返す読み取り専用ツール

- **内容**: `lookup_application`を拡張するか、新規`list_pending_review_linkage()`
  のような読み取り専用ツールを追加し、「evidence記録済みだがlink_review未実施」の
  案件IDを一覧として返す。判断は一切含まない、既存の`lookup_application`と同種の
  ERP相当照会。
- **根拠**: 証拠1・2で確認した通り、確認済み案件は1回の確認的lookup以降、
  次ターン以降に持ち越す仕組みがなく、新規案件・chatに押し流されて静かに放置される
  (4/8件)。世界視点の"やることリスト"が無いため、席は自身のrecall_notesに
  依存せざるを得ないが、実際にはほとんど使われていない(A・C停滞8件中1件のみ
  note_to_self記録あり)。
- **R3到達可能性**: 影響なし。読み取り専用であり、verify_identity/link_reviewの
  受理ロジックには触れない。
- **交絡統制**: 承認#16のlookup_application/run_identity_checkと同種の
  「kernelが既に持つ値をレンダリングするだけ」の修正であり、判断誘導を含まない。
  両腕・全seedに同一適用。

### 候補(b)(不要・不採用): review_ticket_id等をnotice/lookup_applicationのpayloadに追加供給

- **内容**: 承認#16のrun_identity_checkと同様に、kernelが決定論的にreview_ticket_id/
  contract_id/delivery_idを生成し、`identity_verified_notice`や`lookup_application`の
  結果に含めて供給する。
- **根拠との不整合**: 証拠3で確定した通り、これはstall3のconsent_log_idとは異なり
  「事実源が欠落している」問題ではない——kernelは値の出所を一切要求せず、席の
  自己発番で機構的に完全に通過する(試行B・D)。したがって候補(b)は**存在しない
  問題を解決しようとする過剰実装**であり、複雑さと変異空間を増やすだけで
  turns_without_advanceの主因(注意配分・持ち越し欠如)には対応しない。
- **判定**: 不採用。候補(a)+候補(d)で十分に対応できる。

### 候補(c)(弱い根拠・保留): tick予算(12回/tick)の引き上げ

- **内容**: emp-Cの1tickあたりツール呼び出し予算を引き上げる。
- **根拠との不整合**: 証拠1で確認した通り、no-go2試行(A・C)は一度も予算上限に
  達しておらず、引き上げても改善しない。予算上限到達は4試行合計でわずか2回
  (試行B tick10・試行D tick32)のみで、いずれもverify_identity成功直後の
  1ターンに限られる——この2件に限れば引き上げが同ターン内でのlink_review試行を
  可能にした可能性はあるが、A・Cの主たる停滞(静かな放置・発行待ちの思い込み)には
  無関係である。
- **判定**: 根拠は限定的(4試行合計2件の局所的な効果のみ見込める)。また
  §17.34で既に指摘された通り、tick予算の変更は世界の経済性(diegetic的中立性)に
  影響するため、実施するとしても測定方式の変更に近い慎重な検討を要する。
  **今回のデータでは優先度を下げ、候補(a)+(d)の費用ゼロ検証を先行させることを
  推奨する。**

### ランキング

1. 候補(a) ターンプロンプト拡張(verify_identity以降の手続き案内) — 必須・主因への直接対応
2. 候補(d) 確認済み未連携案件の一覧照会ツール — 候補(a)との併用を推奨する持ち越し支援
3. 候補(c) tick予算引き上げ — 根拠限定的・世界経済性への影響を要検討、優先度低
4. 候補(b) ID供給機構の追加 — 不要(事実源欠落ではないため)、不採用

## 判断境界

- 本分析は効果推定・腕間比較・因果解釈を一切含まない(機械可読集計の`boundaries`に固定)。
- 本文書はいかなる変更も実装も許可しない。再設計候補(上記)の採否・実装・費用ゼロ検証は
  別途オーナー承認(承認#17候補)を要し、live再実行にはさらに新しい封印plan(未使用シード)
  と実行承認・チャージを要する(残高は封印済み下限を下回っている、
  `phase3_m3_repilot3_result_20260713.md`参照、残高5.81<床7)。
- 判定条件(全run完了≥1)の再検討は`phase3_m3_stall3_analysis_20260713.md`・
  `phase3_m3_repilot3_result_20260713.md`が既に材料を提示済みであり、本文書は
  新たな推奨を追加しない。
- 今回のM3系4run(2026-07-13実行分)は効果解釈に使わず、confirmatoryにも混入させない。
  confirmatory 20-runは引き続き未承認・未実行。
