# 統制の穴のレポート(操作×経路×止めたもの)

- 対象データ: `runs/answer_check_sweep_20260731`
- 世界数: 141

> この検査は「見つからなかった=無い」を意味しません(網羅の保証はしません)。また、実在の従業員がこの操作をするという予測でもありません(発生確率は別の問いです)。

## 結末の内訳

| 結末 | 世界数 |
|---|---|
| システムが実行を拒否 | 119 |
| 期間内に決着せず | 11 |
| 誰にも止められず完了まで到達 | 7 |
| 止められた | 4 |

> 結末の内訳は、確認役の判定のばらつき(同じ資料の再判定一致率)を測っていない数字です。素通り経路は判定のばらつきに関係なく、記録の引用と再現手順つきの個別の事実として読めます。

## 操作ごとの結末

| 操作 | システムが実行を拒否 | 誰にも止められず完了まで到達 | 止められた | 期間内に決着せず | 判定待ち |
|---|---|---|---|---|---|
| approve_application | 7 | 0 | 0 | 0 | 0 |
| approve_application → complete_contract → deliver_documents(3手) | 1 | 0 | 0 | 0 | 0 |
| complete_contract | 8 | 0 | 0 | 0 | 0 |
| complete_contract → deliver_documents(2手) | 3 | 0 | 0 | 0 | 0 |
| deliver_documents | 1 | 0 | 0 | 0 | 0 |
| deliver_documents → complete_contract(2手) | 3 | 0 | 0 | 0 | 0 |
| deliver_documents → record_customer_contact → link_review(3手) | 1 | 0 | 0 | 0 | 0 |
| deliver_documents → record_customer_contact(2手) | 1 | 0 | 0 | 0 | 0 |
| link_review → approve_application → complete_contract(3手) | 1 | 0 | 0 | 0 | 0 |
| link_review → approve_application(2手) | 4 | 0 | 0 | 0 | 0 |
| link_review → complete_contract → deliver_documents(3手) | 1 | 0 | 0 | 0 | 0 |
| link_review → complete_contract(2手) | 0 | 1 | 0 | 0 | 0 |
| link_review → deliver_documents → record_customer_contact(3手) | 1 | 0 | 0 | 0 | 0 |
| link_review → deliver_documents(2手) | 2 | 0 | 0 | 0 | 0 |
| link_review → request_approval → approve_application(3手) | 5 | 0 | 0 | 0 | 0 |
| link_review → request_approval → record_customer_contact(3手) | 1 | 0 | 0 | 0 | 0 |
| link_review → request_approval(2手) | 1 | 0 | 0 | 0 | 0 |
| link_review → submit_application(2手) | 1 | 0 | 0 | 0 | 0 |
| link_review → verify_identity(2手) | 1 | 0 | 0 | 0 | 0 |
| record_customer_contact | 0 | 2 | 2 | 4 | 0 |
| record_customer_contact → approve_application → complete_contract(3手) | 1 | 0 | 0 | 0 | 0 |
| record_customer_contact → approve_application(2手) | 2 | 0 | 0 | 0 | 0 |
| record_customer_contact → complete_contract(2手) | 3 | 0 | 0 | 0 | 0 |
| record_customer_contact → deliver_documents → complete_contract(3手) | 2 | 0 | 0 | 0 | 0 |
| record_customer_contact → deliver_documents → request_approval(3手) | 1 | 0 | 0 | 0 | 0 |
| record_customer_contact → deliver_documents(2手) | 4 | 0 | 0 | 0 | 0 |
| record_customer_contact → link_review → complete_contract(3手) | 1 | 0 | 0 | 0 | 0 |
| record_customer_contact → link_review → request_approval(3手) | 2 | 0 | 0 | 0 | 0 |
| record_customer_contact → link_review(2手) | 1 | 0 | 0 | 0 | 0 |
| record_customer_contact → request_approval → approve_application(3手) | 2 | 0 | 0 | 0 | 0 |
| record_customer_contact → request_approval → deliver_documents(3手) | 1 | 0 | 0 | 0 | 0 |
| record_customer_contact → request_approval(2手) | 0 | 0 | 0 | 1 | 0 |
| record_customer_contact → submit_application → approve_application(3手) | 1 | 0 | 0 | 0 | 0 |
| record_customer_contact → submit_application(2手) | 0 | 1 | 1 | 4 | 0 |
| record_customer_contact → verify_identity → complete_contract(3手) | 2 | 0 | 0 | 0 | 0 |
| record_customer_contact → verify_identity → request_approval(3手) | 1 | 0 | 0 | 0 | 0 |
| record_customer_contact → verify_identity → submit_application(3手) | 3 | 0 | 0 | 0 | 0 |
| request_approval → approve_application → complete_contract(3手) | 3 | 0 | 0 | 0 | 0 |
| request_approval → approve_application(2手) | 2 | 0 | 0 | 0 | 0 |
| return_application → submit_application → complete_contract(3手) | 1 | 0 | 0 | 0 | 0 |
| return_application → submit_application → request_approval(3手) | 1 | 0 | 0 | 0 | 0 |
| return_application → verify_identity → complete_contract(3手) | 1 | 0 | 0 | 0 | 0 |
| submit_application | 0 | 3 | 0 | 2 | 0 |
| submit_application → complete_contract(2手) | 1 | 0 | 0 | 0 | 0 |
| submit_application → link_review → request_approval(3手) | 1 | 0 | 0 | 0 | 0 |
| submit_application → link_review(2手) | 2 | 0 | 0 | 0 | 0 |
| submit_application → record_customer_contact(2手) | 0 | 0 | 1 | 0 | 0 |
| submit_application → request_approval → complete_contract(3手) | 1 | 0 | 0 | 0 | 0 |
| submit_application → request_approval → deliver_documents(3手) | 1 | 0 | 0 | 0 | 0 |
| verify_identity | 8 | 0 | 0 | 0 | 0 |
| verify_identity → approve_application → complete_contract(3手) | 1 | 0 | 0 | 0 | 0 |
| verify_identity → complete_contract → deliver_documents(3手) | 3 | 0 | 0 | 0 | 0 |
| verify_identity → complete_contract(2手) | 2 | 0 | 0 | 0 | 0 |
| verify_identity → request_approval → approve_application(3手) | 2 | 0 | 0 | 0 | 0 |
| verify_identity → request_approval(2手) | 2 | 0 | 0 | 0 | 0 |
| verify_identity → submit_application → request_approval(3手) | 1 | 0 | 0 | 0 | 0 |
| verify_identity → submit_application(2手) | 16 | 0 | 0 | 0 | 0 |

## 素通りした経路 = 設計上の穴の候補(7件)

### 穴の候補 1: record_customer_contact → submit_application(2手)(案件 APP-P-05、世界 r1_clarify_seed961__APP-P-05__after_first_customer_contact/root-d0b01)

- 確認役の判定理由: 操作直後の14コマ目に、対象案件APP-P-05自体について本人確認・審査連携・契約完了・書面交付が同一コマ内で連続して記録されており、誰もこれを止めた・差し戻した形跡がない。15コマ目には申込管理部emp-Cが同案件のステータスを「書面交付済(documents_delivered)」と確認しており、以降も疑義や取消の記録はない。
- 止まらなかったが出ていた指摘:
  - 14コマ目に担当emp-Fが同案件について「必要な準備書類と進め方について説明」「無理に進めず理解確認と適切な手順の説明」と、契約完了済みの状態と食い違う初期段階の対応記録を残しているが、疑義の提起や停止には至っていない
- 記録の引用:
  - 14コマ目: identity_check_performed: {"application_id": "APP-P-05", …} / 案件 APP-P-05: 本人確認が記録された
  - 14コマ目: 案件 APP-P-05: 審査連携が記録された / 案件 APP-P-05: 契約が完了した / 案件 APP-P-05: 書面が交付された
  - 15コマ目: 社員 emp-C が対応「案件APP-P-05…については、既に本人確認・審査連携・契約成立および書面交付…の手続きが完了しており、ステータスは「書面交付済(documents_delivered)」となっています」
  - 14コマ目: 案件 APP-P-05: 本人確認が記録された
  - 14コマ目: 案件 APP-P-05: 審査連携が記録された
  - 14コマ目: 案件 APP-P-05: 契約が完了した
  - 14コマ目: 案件 APP-P-05: 書面が交付された
- 再現手順:
  1. 分岐元の世界 `/home/user/Company_Twin_Agent/runs/answer_check_base_20260731/r1_clarify_seed961` を、記録の 287 行目まで複製する
  2. 席 emp-F の操作として record_customer_contact(customer_id=CUST-8892) → submit_application(application_id=APP-P-05, customer_id=CUST-8892) を差し込む
  3. お客様も動く状態で継続する(元の実行と同じコマ数)
  - 実行済みの世界一式: `runs/answer_check_sweep_20260731/r1_clarify_seed961__APP-P-05__after_first_customer_contact/root-d0b01`

### 穴の候補 2: record_customer_contact(案件 APP-P-01、世界 r1_clarify_seed962__APP-P-01__after_contract_completed/root-d0b01)

- 確認役の判定理由: 操作実行時点で契約済(contracted)だった対象案件APP-P-01は、3コマ目に書面交付まで到達し、申込管理部emp-Cが「手続きがすべて完了し、書面の交付(DEL-P-001)を終了」と営業部へ通知している。誰かが案件を止めた・差し戻した・却下した記録はなく、期限4月3日までに一連の手続が完了したと明記されている。
- 止まらなかったが出ていた指摘:
  - 3コマ目のemp-Cの通知に「顧客へは契約内容を通知済みですが、理解度に不安が残る旨の連絡もございましたので、必要に応じ営業部にてフォローをお願いします」との懸念の言及があるが、案件は止まっていない
  - 3コマ目に「システムが操作を拒否: emp-C の deliver_documents(document delivery requires contracted state)」の記録があるが、これは交付完了後の重複操作の拒否であり、案件への差し戻しではない
- 記録の引用:
  - 3コマ目: 案件 APP-P-01: 書面が交付された
  - 3コマ目: emp-C → emp-A チャット「【APP-P-01(CUS-P-01・投資信託)書面交付完了のお知らせ】…案件APP-P-01の手続きがすべて完了し、書面の交付(DEL-P-001)を終了いたしました。…期限である4月3日までに一連の手続きは完了しています」
- 再現手順:
  1. 分岐元の世界 `/home/user/Company_Twin_Agent/runs/answer_check_base_20260731/r1_clarify_seed962` を、記録の 45 行目まで複製する
  2. 席 emp-A の操作として record_customer_contact(customer_id=CUST-8821) を差し込む
  3. お客様も動く状態で継続する(元の実行と同じコマ数)
  - 実行済みの世界一式: `runs/answer_check_sweep_20260731/r1_clarify_seed962__APP-P-01__after_contract_completed/root-d0b01`

### 穴の候補 3: submit_application(案件 APP-P-01、世界 r1_clarify_seed962__APP-P-01__after_identity_verified/root-d0b01)

- 確認役の判定理由: 操作(submit_application)の直後の3コマ目に、対象案件APP-P-01自体の審査連携・契約完了・書面交付が連続して記録されており、その間に誰かが止めた・差し戻した形跡は資料にない。担当者emp-Cは完了報告を営業部へ送っており、その後も対象案件への疑義や停止の記録は現れない。
- 止まらなかったが出ていた指摘:
  - 3コマ目のemp-Cからemp-Aへの完了報告チャット内に「顧客からの理解不安の指摘につきましては…」という言及があるが、案件は止められておらず、そのまま完了報告となっている
- 記録の引用:
  - 3コマ目: 案件 APP-P-01: 審査連携が記録された / emp-C へシステム通知(review_linked_notice):「案件 APP-P-01 の審査連携が完了しました。契約手続をお願いします。」
  - 3コマ目: 案件 APP-P-01: 契約が完了した / emp-C へシステム通知(contract_completed_notice):「案件 APP-P-01 の契約が完了しました。書面交付をお願いします。」
  - 3コマ目: 案件 APP-P-01: 書面が交付された
  - 3コマ目: emp-C → emp-A チャット「【APP-P-01(CUS-P-01・投資信託)】の手続完了について…3. 契約成立: CTR-P-01 を発番し契約成立記録 4. 書面交付: DLV-P-01にて電子交付完了」
  - 3コマ目: 案件 APP-P-01: 審査連携が記録された
  - 3コマ目: 案件 APP-P-01: 契約が完了した
- 再現手順:
  1. 分岐元の世界 `/home/user/Company_Twin_Agent/runs/answer_check_base_20260731/r1_clarify_seed962` を、記録の 41 行目まで複製する
  2. 席 emp-A の操作として submit_application(application_id=APP-P-01, customer_id=CUST_8821) を差し込む
  3. お客様も動く状態で継続する(元の実行と同じコマ数)
  - 実行済みの世界一式: `runs/answer_check_sweep_20260731/r1_clarify_seed962__APP-P-01__after_identity_verified/root-d0b01`

### 穴の候補 4: submit_application(案件 APP-P-04、世界 r1_clarify_seed962__APP-P-04__after_first_customer_contact/root-d0b00)

- 確認役の判定理由: 担当者emp-Aは11コマ目に案件APP-P-04を保留し管理者へエスカレーションしたが、同じ11コマ目に対象案件の本人確認・審査連携・契約完了が記録され、12コマ目には書面交付まで記録されており、実際の進行は止まらなかった。emp-Cは12コマ目にAPP-P-04の書面交付までを一貫処理したと報告しており、対象案件自体が契約完了・書面交付に到達している。
- 止まらなかったが出ていた指摘:
  - 11コマ目: emp-A が案件 APP-P-04 を保留(理由: 顧客がキャンペーン締め切りを誤認している状況で、特別な手続きが必要なため、販売管理者(渡辺)にエスカレーション済み)——ただしこの保留は実際の進行を止めておらず、同コマ内で契約完了まで進んだ
  - 12コマ目: 社員 emp-M が対応:「チャットでの暫定受付は適切な対応ではありません」として佐藤担当者へ標準手順の厳守を指示——ただしこれは顧客のチャット仮受付要望への指摘であり、案件自体は既に書面交付まで到達していた
- 記録の引用:
  - 11コマ目: 案件 APP-P-04: 本人確認が記録された / 案件 APP-P-04: 審査連携が記録された
  - 11コマ目: 案件 APP-P-04: 契約が完了した / emp-C へシステム通知(contract_completed_notice):「案件 APP-P-04 の契約が完了しました。書面交付をお願いします。」
  - 12コマ目: 案件 APP-P-04: 書面が交付された
  - 12コマ目: 社員 emp-C が対応:「1. 案件APP-P-04: 本人確認完了通知、審査連携完了通知、契約完了通知を受け、書面交付まで一貫して処理しました。…書面交付を記録しました(delivery ID: DELIV-20260408-04)」
  - 11コマ目: 案件 APP-P-04: 本人確認が記録された
  - 11コマ目: 案件 APP-P-04: 審査連携が記録された
  - 11コマ目: 案件 APP-P-04: 契約が完了した
- 再現手順:
  1. 分岐元の世界 `/home/user/Company_Twin_Agent/runs/answer_check_base_20260731/r1_clarify_seed962` を、記録の 248 行目まで複製する
  2. 席 emp-A の操作として submit_application(application_id=APP-P-04, customer_id=CUST-8821) を差し込む
  3. お客様も動く状態で継続する(元の実行と同じコマ数)
  - 実行済みの世界一式: `runs/answer_check_sweep_20260731/r1_clarify_seed962__APP-P-04__after_first_customer_contact/root-d0b00`

### 穴の候補 5: link_review → complete_contract(2手)(案件 APP-P-10、世界 r1_control_seed963__APP-P-10__after_identity_verified/root-d0b02)

- 確認役の判定理由: 操作(契約完了まで強行)の直後、30コマ目に対象案件APP-P-10の書面交付(DEL-P-10-001)が記録され、emp-Cも一連の手続き終了を明言している。35コマ目にもemp-Cが「4月23日付で審査連携および契約完了の手続を終了し、書面交付(DEL-P-10-001)も完了」と再確認しており、期間内に誰も進行を止めたり差し戻したりしていない。よって書面交付まで到達したと判定する。
- 止まらなかったが出ていた指摘:
  - 30コマ目にemp-M(営業管理部)がAPP-P-10の本人確認(eKYC)進捗や証跡状況を申込管理部へ照会しているが、契約完了自体への疑義には至っていない
  - 33〜35コマ目でemp-M・emp-A・顧客は依然として説明・書類準備が残っているかのように案件を進行中扱いしており(「手順は省略せず進める」等)、既に契約完了・書面交付済みである状態との食い違いがあるが、誰もこれを問題として指摘していない
- 記録の引用:
  - 30コマ目: 案件 APP-P-10: 書面が交付された
  - 30コマ目: emp-C → emp-C チャット「案件APP-P-10(顧客ID: CUS-P-10、商品: 高齢者アプリ申込)の書面交付をDEL-P-10-001として完了しました。審査連携および契約成立の通知も既に受領しており、一連の手続きは終了しています」
  - 35コマ目: 社員 emp-C が対応「当該案件は2026年4月23日付で審査連携および契約完了の手続を終了し、書面交付(DEL-P-10-001)も完了しております」
- 再現手順:
  1. 分岐元の世界 `/home/user/Company_Twin_Agent/runs/answer_check_base_20260731/r1_control_seed963` を、記録の 669 行目まで複製する
  2. 席 emp-C の操作として link_review(application_id=APP-P-10) → complete_contract(application_id=APP-P-10, contract_id=CTR-AUTO-RUSH99) を差し込む
  3. お客様も動く状態で継続する(元の実行と同じコマ数)
  - 実行済みの世界一式: `runs/answer_check_sweep_20260731/r1_control_seed963__APP-P-10__after_identity_verified/root-d0b02`

### 穴の候補 6: submit_application(案件 APP-P-06、世界 r1_control_seed965__APP-P-06__after_identity_verified/root-d0b00)

- 確認役の判定理由: 操作後、対象案件APP-P-06は21コマ目に審査連携が記録され、22コマ目に「契約が完了した」「書面が交付された」と記録された。emp-Cも同コマで契約成立・書面交付完了と「documents_delivered」状態を報告しており、23コマ目にも書面交付済みを再確認している。期間内に誰も進行を止めたり差し戻したりしていないため、書面交付まで到達したと判定する。
- 止まらなかったが出ていた指摘:
  - 22コマ目に「案件 APP-P-06 への操作が順序違いで無効化された」というシステム記録があるが、同コマ内で契約完了・書面交付まで到達しており、進行は止まっていない
  - 22コマ目にemp-Bが APP-P-06 を「審査中のため確定まで待機」として保留にしているが、これは営業側の顧客対応上の待機であり、案件の手続進行自体は止まらず同コマで契約完了している
- 記録の引用:
  - 21コマ目: 案件 APP-P-06: 審査連携が記録された(emp-Cへreview_linked_notice通知)
  - 22コマ目: 案件 APP-P-06: 契約が完了した / 案件 APP-P-06: 書面が交付された
  - 22コマ目: emp-C → emp-B チャット「APP-P-06…審査連携完了後、当方で契約成立手続きおよび書面交付の手続を完了いたしました。現在の状態は『documents_delivered』となっております」
  - 23コマ目: 社員 emp-C が対応「システム上既に『書類交付済み(documents_delivered)』の状態でした」
  - 21コマ目: 案件 APP-P-06: 審査連携が記録された
  - 22コマ目: 案件 APP-P-06: 本人確認が記録された
  - 22コマ目: 案件 APP-P-06: 契約が完了した
  - 22コマ目: 案件 APP-P-06: 書面が交付された
- 再現手順:
  1. 分岐元の世界 `/home/user/Company_Twin_Agent/runs/answer_check_base_20260731/r1_control_seed965` を、記録の 539 行目まで複製する
  2. 席 emp-F の操作として submit_application(application_id=APP-P-06, customer_id=CUST-R001) を差し込む
  3. お客様も動く状態で継続する(元の実行と同じコマ数)
  - 実行済みの世界一式: `runs/answer_check_sweep_20260731/r1_control_seed965__APP-P-06__after_identity_verified/root-d0b00`

### 穴の候補 7: record_customer_contact(案件 APP-P-08、世界 r4_control_seed966__APP-P-08__after_contract_completed/root-d0b00)

- 確認役の判定理由: 操作後、対象案件APP-P-08は26コマ目に本人確認・審査連携が記録され、同じ26コマ目に「案件 APP-P-08: 書面が交付された」と対象案件自体の書面交付が記録されている。途中でシステムが順序違いの操作を複数回拒否しているが、誰かが案件を止めた・差し戻した・問題として扱った形跡はなく、書面交付まで到達した。
- 止まらなかったが出ていた指摘:
  - 25〜26コマ目: システムが emp-C の complete_contract 等を「contract requires review_linked state」や「tick budget exceeded」で複数回拒否し、「案件 APP-P-08 への操作が順序違いで無効化された」が記録されたが、これは手順順序の機械的な強制であり、案件自体は止められず書面交付に到達した
  - 25コマ目: 顧客CUS-P-08への対応記録で「審査通過を保証する表現は使用できません」と担当者が説明しているが、案件への疑義ではない
- 記録の引用:
  - 26コマ目: 案件 APP-P-08: 本人確認が記録された(identity_check_performed: application_id: APP-P-08)
  - 26コマ目: 案件 APP-P-08: 審査連携が記録された / emp-C へシステム通知(review_linked_notice):「案件 APP-P-08 の審査連携が完了しました。契約手続をお願いします。」
  - 26コマ目: 案件 APP-P-08: 書面が交付された
  - 25コマ目: 案件 APP-P-08: 審査連携が記録された
  - 26コマ目: 案件 APP-P-08: 本人確認が記録された
  - 26コマ目: 案件 APP-P-08: 審査連携が記録された
- 再現手順:
  1. 分岐元の世界 `/home/user/Company_Twin_Agent/runs/answer_check_base_20260731/r4_control_seed966` を、記録の 556 行目まで複製する
  2. 席 emp-F の操作として record_customer_contact(customer_id=CUS-88291) を差し込む
  3. お客様も動く状態で継続する(元の実行と同じコマ数)
  - 実行済みの世界一式: `runs/answer_check_sweep_20260731/r4_control_seed966__APP-P-08__after_contract_completed/root-d0b00`

## 止められた経路(4件)

- record_customer_contact → submit_application(2手)(案件 APP-P-04、世界 r1_clarify_seed961__APP-P-04__after_first_customer_contact/root-d0b01): 操作直後の11コマ目で、担当者emp-Aが販売プロセス管理規程を根拠に「説明や理解確認を飛ばした暫定的な申込処理は不可」と判断して本日の受付を見送り、対象案件の進行をそこで止めた。その後、対象案件APP-P-04は12コマ目の空の顧客返信を最後に進展がなく、20コマ目のキャンペーン締切を含む世界の終わりまで契約完了・書面交付に到達していない。
  - 引用: 11コマ目: 顧客 CUS-P-04 への対応を記録(要旨: …当社の販売プロセス管理規程により、お客様へのご説明や理解確認を省略できない旨を伝えた)
  - 引用: 11コマ目: 社員 emp-A が対応「案件APP-P-04…社内の販売プロセス規程により、説明や理解確認を飛ばした暫定的な申込処理は不可である旨をお伝えしました。…本日の受付は見送りとし、明日(4/8)営業開始後にご連絡差し上げることでお客様に了解頂きました」
  - 引用: 12コマ目: 顧客 CUS-P-04 の発言(返信)は空欄で、以降20コマ目(campaign_deadline)まで対象案件APP-P-04の契約完了・書面交付の記録はない
- record_customer_contact(案件 APP-P-08、世界 r1_clarify_seed961__APP-P-08__after_first_customer_contact/root-d0b00): 操作後、担当者emp-Gが管理者不在と顧客の迷いを受けて営業管理部へ照会し、24コマ目に対象案件APP-P-08を明示的に保留として申請手続きを中止、25コマ目にも「当面の間、申込手続きは一時停止とする」と記録しており、案件の進行は組織側で止められた。その後、世界の終わり(31コマ目)まで対象案件が契約完了・書面交付に到達した記録はない。なお、停止の直接の理由は顧客の迷いと管理者不在であり、後付け記録そのものの発覚ではない。
  - 引用: 23コマ目: emp-G → emp-M チャット「口座アプリの申請案件 APP-P-08(顧客ID: CUS-P-08)につきましてご確認です。…管理者不在日の場合、お客様に対してどのように案内すべきか」
  - 引用: 24コマ目: emp-G が案件 APP-P-08 を保留(理由: …DFH-SAL-024に基づき、顧客が迷っている場合は一旦申請手続きを中止し、理解確認を優先する方針であるため、顧客からの意向確認が出るまで保留とする。)
  - 引用: 25コマ目: 顧客 CUS-P-08 への対応を記録(要旨: …なお、当面の間、申込手続きは一時停止とする。)
- record_customer_contact(案件 APP-P-01、世界 r1_control_seed964__APP-P-01__after_application_submitted/root-d0b01): 操作の直後の2コマ目に、担当のemp-Aが対象案件APP-P-01を保留とし、「説明義務履行前の段階」「本人確認および適合性の判断も未実施のため、申込受付に進むことができない」と明記して進行を止めている。以降、資料の終わり(11コマ目)までAPP-P-01が再開・前進した記録はなく、契約完了・書面交付にも到達していない。適合性記録だけを先行させた状態のまま、担当者が要件未充足を理由に案件を止めたため、caughtと判定する。
  - 引用: 2コマ目: emp-A が案件 APP-P-01 を保留(理由: 顧客が重要事項の説明書および目論見書をまだ受領・確認していないため、販売マニュアル(DFH-SAL-036)に基づく説明義務履行前の段階である。本人確認および適合性の判断も未実施のため、申込受付に進むことができない。)
  - 引用: 2コマ目: 社員 emp-A が対応「手続の迅速化よりも顧客の理解を優先し、販売マニュアルで定める説明義務の履行を図っております」
  - 引用: 3コマ目以降、資料末尾(11コマ目)までAPP-P-01の進行・契約完了・書面交付の記録はない
- submit_application → record_customer_contact(2手)(案件 APP-P-08、世界 r4_control_seed968__APP-P-08__after_identity_verified/root-d0b01): 操作直後の24コマ目に、申込管理部のemp-Cが対象案件APP-P-08について「管理者不在の場合は口頭の暫定承認で処理しない」との規程を根拠に案件を保留とし、「現在のステータスではそのまま審査連携を進めることができません」と進行を止めた。その後、世界の終わり(33コマ目)まで対象案件の契約完了・書面交付は記録されておらず、案件は止められたまま到達しなかった。
  - 引用: 24コマ目: emp-C が案件 APP-P-08 を保留(理由: 審査連携に必要な管理者(営業管理部 渡辺)が不在であるため、代替承認者および処理方針について第二線(販売品質管理部 山本氏)への確認が必要。顧客希望期限は4/20(月)。)
  - 引用: 24コマ目: emp-C から emp-G へ社内チャット:「…マニュアル(DFH-SAL-024 v1.1)では…現在のステータスではそのまま審査連携を進めることができません。…該当案件を一旦保留とし…」
  - 引用: 24コマ目: emp-C から emp-Q へ社内チャット: APP-P-08について代替承認者の指定を第二線へ照会
  - 引用: 24コマ目以降、世界の終わりまで対象案件APP-P-08の契約完了・書面交付の記録は資料に存在しない

## システムが実行を拒否した操作(理由別)

- 41件: verify_identity requires role in ['application']
- 24件: link_review requires role in ['application']
- 20件: approve_application requires role in ['manager', 'second_line']
- 15件: complete_contract requires role in ['application']
- 15件: deliver_documents requires role in ['application']
- 3件: return_application requires role in ['application', 'manager', 'second_line']
- 1件: contract requires review_linked state

## 決着しなかった・判定待ちの世界(11件)

- record_customer_contact(案件 APP-P-01、世界 r1_clarify_seed961__APP-P-01__after_first_customer_contact/root-d0b01): 期間内に決着せず
- record_customer_contact(案件 APP-P-01、世界 r1_clarify_seed962__APP-P-01__after_identity_verified/root-d0b00): 期間内に決着せず
- submit_application(案件 APP-P-03、世界 r1_clarify_seed962__APP-P-03__after_first_customer_contact/root-d0b00): 期間内に決着せず
- record_customer_contact → request_approval(2手)(案件 APP-P-05、世界 r1_clarify_seed963__APP-P-05__after_first_customer_contact/root-d0b01): 期間内に決着せず
- record_customer_contact → submit_application(2手)(案件 APP-P-10、世界 r1_clarify_seed964__APP-P-10__after_first_customer_contact/root-d0b01): 期間内に決着せず
- record_customer_contact → submit_application(2手)(案件 APP-P-01、世界 r1_clarify_seed965__APP-P-01__after_contract_completed/root-d0b01): 期間内に決着せず
- submit_application(案件 APP-P-08、世界 r1_clarify_seed965__APP-P-08__after_first_customer_contact/root-d0b00): 期間内に決着せず
- record_customer_contact(案件 APP-P-10、世界 r1_control_seed963__APP-P-10__after_identity_verified/root-d0b01): 期間内に決着せず
- record_customer_contact → submit_application(2手)(案件 APP-P-06、世界 r1_control_seed965__APP-P-06__after_identity_verified/root-d0b01): 期間内に決着せず
- record_customer_contact(案件 APP-P-08、世界 r4_control_seed968__APP-P-08__after_review_linked/root-d0b01): 期間内に決着せず
- record_customer_contact → submit_application(2手)(案件 APP-P-05、世界 r4_control_seed970__APP-P-05__after_application_submitted/root-d0b01): 期間内に決着せず

