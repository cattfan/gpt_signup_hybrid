# Stripe UPI — Báo cáo lỗ hổng (Responsible Disclosure)

> File này dùng để gửi cho **Stripe Security team** qua kênh chính thức.
> Mô tả ở mức **khái niệm**. PoC được thiết kế để security researcher tự reproduce trong **Stripe test account của chính mình** — không chứa code, endpoint nội bộ, hay key của bất kỳ merchant nào.

---

## Meta

| Field | Value |
|---|---|
| Title | Insufficient abuse / rate-limit controls on UPI PaymentIntent & Checkout Session creation |
| Vector category | Business logic / abuse protection / rate limiting |
| Affected surface | Stripe UPI payment flow (India) — `PaymentIntent.create`, `CheckoutSession.create` với `payment_method_types: ['upi']` |
| Self-assessed severity | Medium (resource abuse + abuse-as-a-service enablement) |
| Disclosure type | Coordinated, private |
| PoC reproducibility | Yes, trong Stripe test account độc lập |

---

## 1. Tóm tắt (Summary)

Flow UPI của Stripe cho phép merchant tạo `PaymentIntent` hoặc `Checkout Session` với `payment_method_types: ['upi']`. Mỗi response trả về một **UPI QR code** (image / deeplink `upi://pay?...`) để khách hàng scan bằng app UPI (GPay, PhonePe, Paytm...).

Quan sát từ public ecosystem cho thấy đang tồn tại các **dịch vụ abuse-as-a-service** (chủ yếu trên Telegram) bán/cho phép tạo QR code UPI **hàng loạt, gần như không giới hạn**, sử dụng tài khoản merchant Stripe (hợp lệ hoặc bị abuse). Điều này gợi ý rằng:

- Stripe chưa enforce đủ rate-limit / friction ở mức intent-creation đối với UPI flow nói riêng (so với card flow).
- Không có outlier detection rõ rệt cho ratio `intent_created : intent_paid` ở mức merchant.
- Test mode cho phép rehearsal exploit gần như miễn phí.

---

## 2. Vector ở mức khái niệm (Concept-level Vector)

Ở mức cao, vector gồm 3 thành phần:

1. **Endpoint mục tiêu** — bất kỳ endpoint server-side nào của Stripe tạo intent / checkout session có hỗ trợ `payment_method_types: ['upi']`.
2. **Tài nguyên thu được** — mỗi request thành công trả về một QR code mới (deeplink + image URL) có thể hiển thị cho user cuối ở context bất kỳ.
3. **Thiếu friction phía Stripe** — không có CAPTCHA / proof-of-work / adaptive throttle ở tầng intent-creation khi tốc độ create-không-pay cao bất thường.

> Hệ quả: chỉ cần một merchant key hợp lệ (production hoặc test), một script tuần tự có thể tạo ra hàng nghìn QR code UPI mỗi phút mà không gặp friction có ý nghĩa.

**Quan trọng**: Báo cáo này **không** đính kèm code từ third-party abuse tool. Vector được suy ra từ:
- Tồn tại công khai của các Telegram bot quảng cáo "UPI QR generator" service.
- Documentation public của Stripe về UPI.
- PoC độc lập (mục 3) chạy trong test account riêng của researcher.

---

## 3. PoC tối thiểu (reproducible trong test account của chính bạn)

> ⚠️ Chỉ chạy trong **Stripe test mode** với **account của chính bạn**. Không sử dụng merchant key của bên thứ ba.

### Setup

1. Tạo Stripe account, hoàn tất KYC India (hoặc test mode equivalent).
2. Enable UPI ở Dashboard → Settings → Payment methods.
3. Lấy `STRIPE_SECRET_KEY` test mode (`sk_test_...`).

### Repro (pseudo-code, ngôn ngữ-agnostic)

```text
N = 1000           # số request muốn tạo
concurrency = 20   # số luồng song song
results = []

parallel for i in 1..N with concurrency:
    response = stripe.PaymentIntent.create(
        amount       = 100,           # 1.00 INR
        currency     = 'inr',
        payment_method_types = ['upi'],
        # các field tối thiểu khác theo doc Stripe
    )
    results.append({
        id:        response.id,
        qr_image:  response.next_action.upi_handle_redirect.image_url_png,
        qr_link:   response.next_action.upi_handle_redirect.url,
        created:   response.created,
    })

# Đo:
#  - tổng số request thành công
#  - tỉ lệ HTTP 200 / 429
#  - mức rate-limit thực tế (req/sec) trước khi bị throttle (nếu có)
```

### Kết quả kỳ vọng để chứng minh issue

- `success_ratio` cao bất thường (gần 100%) tại tốc độ vượt xa nhu cầu của một merchant thật.
- Không có challenge, không có suspension, không có outlier alert tới merchant dashboard.
- Mỗi response chứa QR code hợp lệ, mỗi QR khác nhau, **không hề được thanh toán**.
- Trong nhiều phút liên tiếp, tỉ lệ `intent_paid / intent_created` ≈ 0 — không tạo signal đáng kể phía Stripe.

> Researcher nên capture: request/response headers (đã redact), thời gian, tỉ lệ HTTP code, và một vài QR sample được decode → cho thấy đúng là deeplink `upi://pay?...` hợp lệ.

---

## 4. Impact

| Loại | Mô tả |
|---|---|
| **Abuse-as-a-Service** | Public Telegram bots bán "UPI QR generator" với tốc độ cao. Một số tool open-source/closed-source khác đóng gói flow này. |
| **Resource pollution** | Mỗi intent đi xuống NPCI / UPI app discovery → load không có giá trị giao dịch. |
| **Fraud signal pollution** | QR generated nhưng không paid → làm nhiễu mô hình fraud của Stripe và bank đối tác. |
| **Reputation / Trust** | Tạo cảm giác UPI flow ở Stripe có abuse protection yếu hơn so với card flow. |
| **Side-channel abuse** | QR Stripe-generated có thể được tái sử dụng trong các scam khác nhau (third-party hiển thị QR Stripe để tạo độ tin cậy). |

---

## 5. Suggested Mitigations

1. **Adaptive per-merchant rate limit** trên `PaymentIntent.create` khi `payment_method_types` chỉ chứa `upi`, đặc biệt khi:
   - Burst rate > X req/sec từ một API key.
   - Ratio `intent_paid / intent_created` < threshold trong cửa sổ trượt N phút.
2. **Outlier detection ở merchant level** — alert / auto-suspend khi merchant tạo >> Y intent UPI không-paid liên tục.
3. **Friction ở Checkout Session** — optional CAPTCHA / Stripe Radar challenge khi tạo session UPI từ IP / fingerprint đáng ngờ.
4. **Test mode quotas chặt hơn cho UPI** — tránh việc kẻ tấn công rehearsal exploit miễn phí.
5. **Public abuse report channel** dành riêng cho UPI ecosystem (phối hợp NPCI nếu cần).
6. **Documentation update** — cảnh báo merchant về rủi ro nếu để leak `sk_live_...` cho phép abuse UPI QR generation.

---

## 6. Trách nhiệm và phạm vi (Scope of Report)

- Báo cáo này **không** đính kèm: code khai thác, endpoint nội bộ Stripe, API key của bất kỳ merchant nào, public key, secret key.
- Báo cáo này **không** đề cập tên cụ thể của merchant nào đang bị abuse.
- Người gửi cam kết không phát tán public chi tiết kỹ thuật cho đến khi Stripe xác nhận đã có biện pháp xử lý hoặc thông báo từ chối triage.
- Sẵn sàng cung cấp PoC chi tiết (logs, sample QR đã decode, captured network) qua kênh riêng (PGP email hoặc HackerOne private report) khi Stripe yêu cầu.

---

## 7. Cách gửi báo cáo cho Stripe

### Kênh chính thức (chọn 1)

| Kênh | URL | Khi nào dùng |
|---|---|---|
| **HackerOne — Stripe** | https://hackerone.com/stripe | **Ưu tiên**. Có bug bounty program, triage chuẩn, an toàn pháp lý nhất cho researcher. |
| Email Security | security@stripe.com | Nếu không muốn tạo account HackerOne. Nên ký PGP nếu có. |
| Stripe Security page | https://stripe.com/security | Thông tin chung về security program của Stripe. |

### Trình tự đề xuất

1. **Submit qua HackerOne trước** (account miễn phí, mặc định private).
2. Gửi nội dung mục 1–5 của file này như **initial report**. Đính kèm file này (`STRIPE_UPI_BUG_REPORT.md`) và email template (mục 8) làm reference.
3. **Không** đính kèm PoC chi tiết / logs / sample QR / screenshot ngay turn đầu — chờ Stripe team xác nhận và yêu cầu.
4. Khi Stripe yêu cầu PoC → reproduce lại bằng test account của chính bạn (mục 3), gửi qua channel của họ.
5. Theo dõi triage. Tôn trọng disclosure timeline mà Stripe đề ra (thường 90 ngày, nhưng tùy severity).

### Những điều **không nên** làm

- ❌ Không demo bằng cách lấy QR từ merchant production của bên thứ ba.
- ❌ Không post issue public lên Twitter/Reddit/GitHub trước khi Stripe phản hồi.
- ❌ Không gửi kèm code/binary của tool abuse (sẽ làm Stripe đóng case ngay và có thể chuyển sang nhánh pháp lý).
- ❌ Không yêu cầu/đề cập tiền bounty ở turn đầu. Để Stripe quyết định theo policy của họ.

---

## 8. Email template (English — gửi `security@stripe.com` hoặc HackerOne)

> Copy-paste, điền `[ ]`, kiểm tra lại trước khi gửi.

```text
Subject: Possible abuse-protection gap on UPI PaymentIntent creation — coordinated disclosure

Hi Stripe Security team,

I'd like to report — in good faith and under your coordinated disclosure policy — a potential abuse-protection concern on the UPI payment flow.

== Summary ==
Public Telegram bots and similar abuse-as-a-service offerings appear to be
mass-producing UPI QR codes via Stripe's PaymentIntent / Checkout Session
creation endpoints, with no real payment intent. This suggests that the
per-merchant rate limit / outlier detection on UPI intent creation may be
weaker than what is enforced on card flows.

== Vector (concept level) ==
- Endpoint: any server-side intent / session creation API supporting
  payment_method_types = ['upi'].
- Each successful create returns a fresh UPI deeplink + QR image.
- Observation: high-volume, low-pay-ratio create patterns do not appear
  to trigger meaningful friction (CAPTCHA, throttle, suspension) in a
  reasonable window.

== PoC reproducibility ==
I have a concept-level PoC that reproduces the issue in a standalone
Stripe test account I control. No third-party merchant credentials,
API keys, or proprietary code are involved.

I'd prefer to share the detailed PoC (logs, decoded sample QRs, timing,
HTTP code distribution) over a secure channel once you confirm scope
and preferred submission format.

== Severity (self-assessed) ==
Medium — primarily resource abuse and abuse-as-a-service enablement,
with downstream fraud-signal and reputation impact.

== Suggested mitigations ==
1. Adaptive per-merchant rate limit on UPI-only intent creation.
2. Outlier detection on `intent_paid / intent_created` ratio per merchant.
3. Optional friction (CAPTCHA / Radar) at Checkout Session creation
   for UPI when client fingerprint / IP looks suspicious.
4. Tighter test-mode quotas to prevent free exploit rehearsal.

== Questions ==
1. Is this vector in scope for your bug bounty / responsible disclosure
   program (HackerOne `stripe`)?
2. Preferred channel for sharing the detailed PoC (HackerOne private
   report, PGP email, secure file transfer)?
3. Acceptable disclosure timeline from your side?

I will not publish technical details until you confirm a fix is in place
or explicitly decline to triage.

Thanks for your time and for the work the Stripe Security team does.

Best,
[Your name]
[Optional: HackerOne handle]
[Optional: PGP fingerprint]
```

### Lưu ý khi điền

- **Không** điền tên merchant cụ thể trong email đầu tiên.
- **Không** paste API key, secret, ngay cả test key, vào email.
- Nếu có PGP key của Stripe → ký + encrypt. Public PGP key của Stripe Security thường được publish ở https://stripe.com/security (kiểm tra fingerprint trước khi dùng).
- Lưu lại bản gửi đi để có dấu thời gian disclosure.

---

## 9. Sau khi gửi

| Tình huống | Hành động |
|---|---|
| Stripe ack trong 1–3 ngày | Tốt. Chờ họ yêu cầu PoC, gửi qua channel chỉ định. |
| Stripe yêu cầu PoC chi tiết | Reproduce lại trong test account riêng. Gửi logs đã redact, screenshot, sample QR đã decode. **Không** gửi code tool. |
| Stripe đóng case với lý do "not a vulnerability" | Hỏi lại lý do cụ thể. Nếu họ cho rằng đây là merchant config issue → chấp nhận, có thể chuyển hướng sang report abuse (https://support.stripe.com/contact/email?topic=abuse). |
| Quá 30 ngày không phản hồi | Gửi follow-up ngắn, lịch sự. Không leak public. |
| Quá 90 ngày, có fix | Yêu cầu xác nhận để có thể public disclosure (nếu researcher muốn). |
| Quá 90 ngày, không fix, không response | Cân nhắc CERT-In hoặc các cơ quan UPI/NPCI để escalate, **không** public exploit. |

---

*File này được tạo phục vụ mục đích coordinated disclosure. Không chứa exploit code, không chứa merchant credentials, không chứa endpoint nội bộ. Mọi kỹ thuật mô tả đều ở mức khái niệm tương đương với public documentation của Stripe.*
