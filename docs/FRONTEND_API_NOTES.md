# KaziHub API — notes for the frontend

Answers to the frontend requirements page (asks 1–41). Everything below is live in the code on this branch. `/openapi.json` carries the request and response shapes; this file covers what OpenAPI can't: flows, the WebSocket, and behaviour.

All paths are under `/api/v1` unless shown otherwise.

## Breaking changes to pick up

| Area | Before | Now |
| --- | --- | --- |
| `POST /bookings/fixed` | `service_title` + `amount` from the client | `service_id` required; title and price come from the service. `service_title`/`amount` are ignored. Only `pricing_type: "fixed"` services with a price above 0 (ask 24). |
| `POST /auth/register` | No terms field; free-text fields | `terms_version` required. Names, phone, state and password validated (below). |
| Passwords | No rule (change-password: 8) | 8–128 characters on register, reset and change (ask 16). The app's 6-character minimum needs raising to 8. |
| Login errors | `detail` was sometimes an object | Always `{"detail": "...", "code": "..."}` (ask 41). Suspended/deleted accounts now return 423, not 400. |
| `Idempotency-Key` missing | 400 | 422 (it's now a declared, required header). |
| Free-text fields | Anything saved | 422 for values outside the rules below (asks 3, 20, 37). |
| `is_available_now` | Defaulted to `true` | Means "online right now" and follows `availability_status` (ask 4). |
| Chat `message_type` | Anything | One of `text`, `image`, `audio`, `voice`, `video`, `location`; otherwise 422 (ask 33). |
| `POST /auth/freeze-me` / `unfreeze-me` | Always 200 | 409 when already in that state (ask 18). |

## Field rules (ask 37)

| Field | Rule |
| --- | --- |
| `first_name`, `last_name` | 1–40 letters, spaces, `-`, `'`, `.` |
| `phone_number` | `^\+234[789]\d{9}$`. `0803…` and `234803…` are accepted and stored as `+234803…`. |
| `state` | One of the 36 states or `Abuja (FCT)`: Abia, Adamawa, Akwa Ibom, Anambra, Bauchi, Bayelsa, Benue, Borno, Cross River, Delta, Ebonyi, Edo, Ekiti, Enugu, Gombe, Imo, Jigawa, Kaduna, Kano, Katsina, Kebbi, Kogi, Kwara, Lagos, Nasarawa, Niger, Ogun, Ondo, Osun, Oyo, Plateau, Rivers, Sokoto, Taraba, Yobe, Zamfara. **Please check your 37-item list spells these the same way.** |
| `category` (profile, service, portfolio) | One of the 16 names on your page, exact spelling. |
| `tagline` / `bio` | ≤ 60 / ≤ 500 characters |
| `neighborhood` | ≤ 50 characters, not equal to the state |
| `response_time` | Within 15 minutes, Within 30 minutes, Within 1 hour, Within 3 hours, Within 6 hours, Within a day — or `null` |
| `duration_estimate` | Under 1 hr, 1 hr, 1-2 hrs, 2-3 hrs, 3-5 hrs, Half a day, Full day, 2-3 days, 1 week+ |
| `skills` | ≤ 12, each 1–30 characters, no duplicates |
| `years_of_experience` | 0–60 |
| `base_price` (profile), `price` (service) | > 0 unless `pricing_type` is `quote_required` |
| `preferred_language` / `theme` | `en`, `yo`, `ig`, `ha`, `fr` / `light`, `dark`, `system`. Old labels are reported as codes. |
| `document_number` | NIN 11 digits; passport a letter + 8 digits; voter's card 19 characters; driver's licence 3 letters + 8–9 characters |
| `scheduled_date` / `scheduled_window` | Not in the past / `HH:MM-HH:MM`, start before end |

We kept the human-readable phrases for `response_time` and `duration_estimate`, so the app can keep sending what it sends today.

## Artisan directory (asks 1–6)

- `GET /profiles/` and `GET /profiles/{id}` now include `first_name`, `last_name` and `profile_picture`. You can stop writing the name into `business_name`.
- Profiles with no category yet are never listed, and `meta.total`/`has_more` count only listed profiles.
- Availability, one meaning per field:
  - `is_available`: accepting new work (the artisan's toggle).
  - `is_paused`: account frozen; never listed.
  - `availability_status`: online right now, `Available` / `Busy` / `Offline`.
  - `is_available_now`: always equal to `availability_status == "Available"`.
- Filters: `available_only=true` returns artisans accepting work, online or not. `online_now=true` returns those online right now.
- **Decision (ask 5): unverified artisans are listed.** Show the badge when `is_verified` is true. `verified_only=true` exists if you ever want the strict view.
- **Keep-alive (ask 6):** `GET /health` returns `{"status": "ok", "time", "paystack_mode"}`. A GitHub Actions workflow (`.github/workflows/keep-alive.yml`) pings it every 10 minutes.

## Account and security

- **Frozen accounts (asks 17, 18):** while frozen, every endpoint that changes data returns `423 {"code": "account_frozen"}`. The exceptions are sign-in, refresh, change-password, session management, logout, `freeze-me` and `unfreeze-me`. This applies to every role. Messages are refused if either person is frozen (`423 recipient_unavailable`). A frozen artisan's `GET /profiles/{id}` is 404. `GET /auth/me` reports `is_paused`.
- **Sign out this device (ask 8):** `POST /auth/logout {"refresh_token"}` returns 204. It ends that session only, and its access token stops working immediately.
- **Current device (ask 9):** access tokens carry `sid`. `GET /auth/sessions` items have `session_id` and `is_current`.
- **Signed-out devices (ask 19):** a revoked session's access token gets `401 session_signed_out` immediately. Refreshing it returns `"This session was signed out."` with the same code. `refresh_token_reused` is kept for a genuinely replayed token.
- **2FA (ask 10):**
  - `POST /auth/2fa/verify` now returns `backup_codes`: 10 single-use codes, shown once. Each is 8 characters `0-9`/`A-F`, shown as `7F3A-9C21`. They're accepted with or without the hyphen, in any case (ask 46).
  - `POST /auth/2fa/disable {"current_password", "totp_code"}` accepts an authenticator code or a backup code.
  - `POST /auth/2fa/backup-codes {"totp_code"}` issues a fresh set.
  - Admins can't turn 2FA off.
- **Login and 2FA (ask 41):** every 401 from `POST /auth/login` has a `code`:
  - `invalid_credentials`: wrong email or password.
  - `totp_required`: password correct, no code sent.
  - `totp_invalid`: wrong or expired code.

  The 2FA codes are only returned after the password check passes. `totp_code` also accepts a backup code.
- **Customer privacy (ask 11):** customers now have `phone_visibility` and `share_neighborhood` on `PUT /auth/me` / `GET /auth/me`, with the same values as artisans. `phone_visibility` is an enum in the schema (`after_escrow`, `verified_only`, `hidden`), and its description in `/openapi.json` spells out each value. Enforcement:
  - `phone_visibility`:
    - `after_escrow`: the number appears on the other party's booking (`client_phone` / `artisan_phone`) once the booking is paid into escrow.
    - `verified_only`: the same, but only for an ID-verified viewer. For a customer's number, that means a verified artisan.
    - `hidden`: never shown.
  - `share_neighborhood: false` hides a customer's state on public reviews.
- **Terms (ask 38):** `terms_version` is required on register. It's stored with `terms_accepted_at` (server time) and returned on `GET /auth/me`.

## Notifications (asks 12, 31)

- Created for every booking and chat event. `booking_id` is set whenever there is a booking. `GET /notifications/types` lists every `type` with its meaning:
  - booking flow: `booking_requested`, `quote_sent`, `quote_accepted`, `booking_accepted`, `booking_declined`, `escrow_funded`, `job_started`
  - completion and payment: `completion_submitted`, `payment_released`, `escrow_auto_released`
  - problems: `booking_cancelled`, `booking_disputed`, `dispute_resolved`, `payout_issue`
  - other: `new_message`, `new_review`, `verification_review`, `support_ticket_update`
- A `new_message` notification is created only when the recipient doesn't have that chat open over the WebSocket.
- `GET`/`PUT /notifications/preferences` stores `{"push_enabled", "email_summaries"}`. With `email_summaries` on, the worker emails a daily digest of unread notifications at 07:00 Lagos time.
- **Web Push (ask 44):** with `push_enabled` on, every notification is also pushed to the user's registered devices.
  1. `GET /notifications/push/public-key` returns `{"enabled", "public_key"}`. `enabled` is false until the server has its keys.
  2. In the service worker's page, call `registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: public_key})`.
  3. `POST /notifications/push/subscriptions` with `subscription.toJSON()` as is.
  4. `DELETE /notifications/push/subscriptions {"endpoint"}` on sign-out or when the user turns it off.

  Each push payload is JSON `{"notification_id", "type", "title", "body", "booking_id"}` for the service worker's `push` handler to display. Expired subscriptions are removed automatically.

## Bookings and escrow (asks 23–30)

- New booking fields:
  - `scheduled_date` and `scheduled_window` on both create requests.
  - `client_name`, `client_avatar`, `client_phone`, `artisan_name`, `artisan_avatar`, `artisan_phone`, `artisan_profile_id` and `artisan_category` on every booking response.
- `POST /bookings/upload` (multipart `file`, JPEG/PNG/WebP ≤ 10 MB) returns `{"url"}`. Use it for booking `attachments`, `completion_photos`, dispute `evidence_photos` and review photos.
- `POST /bookings/{id}/start` (artisan) moves `escrow_funded` to `in_progress`. `submit-completion` also works straight from `escrow_funded`.
- `submit-completion` accepts `{"completion_description", "completion_photos"}`.
- `confirm-completion`, `/wallet/release-escrow` and the auto-release job refuse anything that isn't `completed_by_artisan` + `held_in_escrow`. They do this before touching the ledger or Paystack (ask 23).

### Paying into escrow (ask 25)

1. `POST /wallet/initialize-escrow/{booking_id}`, with the booking `pending` or `accepted` and unfunded. Returns `{"authorization_url", "access_code", "reference", "callback_url"}`.
2. Redirect to `authorization_url`, or use `access_code` with Paystack's inline JS.
3. Paystack returns the client to `callback_url`. That's the backend's `PAYSTACK_CALLBACK_URL` setting plus `?booking_id=…`; Paystack appends `&reference=…&trxref=…`. **Tell us the URL of the page you'll host** and we'll set it.
4. That page calls `POST /bookings/{booking_id}/fund-escrow` with an `Idempotency-Key`. If the webhook hasn't arrived yet, the backend asks Paystack directly. It returns the booking as `escrow_funded`, or 400 if Paystack hasn't confirmed the payment.
5. The webhook funds the booking anyway, so a client who closes the tab is still covered.

`GET /health` → `paystack_mode` says `test` or `live` for the deployed keys. `Idempotency-Key` is now a declared header on every endpoint that needs it.

## Chat (asks 32, 35, 36, 39)

- Every timestamp is UTC with an explicit offset (`...Z`) and set per row. Messages come back oldest first.
- `GET /conversations` items now include `client_name`, `client_avatar`, `artisan_name`, `artisan_avatar`, `artisan_profile_id` and `unread_count`.
- `DELETE /conversations/{id}` hides the conversation and clears its history for the caller only. A new message brings it back, showing messages from then on.
- `POST /chat/upload-media`:

  | Kind | Types | Limit |
  | --- | --- | --- |
  | Images | `image/jpeg`, `image/png`, `image/webp` | 10 MB |
  | Video | `video/mp4` | 50 MB |
  | Audio | `audio/webm`, `audio/wav`, `audio/mp4`, `audio/x-m4a`, `audio/m4a`, `audio/aac` | 15 MB |

  It returns `{"url", "media_type", "original_url"}`. For voice notes, `url` is an AAC `.m4a` that plays everywhere, so Safari can upload its recording as is.
- `audio_wave_data` is stored and returned unchanged: up to 200 values, each 0–1.

### WebSocket protocol

The same protocol, including close codes, is in the description of `POST /chat/ws-ticket` in `/openapi.json`, so you don't need this repository to read it (ask 32).

1. `POST /chat/ws-ticket` returns `{"ticket", "expires_in": 30, "websocket_path"}`. Tickets are single-use; fetch a new one per (re)connect.
2. Open `wss://<host>/api/v1/chat/ws/{conversation_id}?ticket={ticket}`. A bad ticket, unknown conversation or non-participant is closed with code 1008.

Client → server:

| Action | Body |
| --- | --- |
| send | Any `MessageCreate` fields, e.g. `{"content": "hi"}`. `"action": "send"` is optional. |
| mark read | `{"action": "mark_read"}` |
| typing | `{"action": "typing", "is_typing": true}` |
| keep-alive | `{"action": "ping"}` |

Server → client (every frame has `event`):

| Event | Payload |
| --- | --- |
| `new_message` | `message` (same shape as REST `MessageResponse`) |
| `message_delivered` | `conversation_id`, `message_id`: the recipient had the chat open |
| `messages_read` | `conversation_id`, `reader_id` |
| `typing` | `conversation_id`, `user_id`, `is_typing` |
| `pong` | — |
| `error` | `code`, `detail`. The connection stays open. Codes: `invalid_json`, `unknown_action`, `validation_error`, `account_frozen`, `recipient_unavailable`, `rejected` |
| booking events | `booking_updated`, `quote_received`, `quote_accepted`, `escrow_funded`, `escrow_released`, `booking_status_changed` |

Message `status` goes `sent` → `delivered` (if the recipient was connected live) → `read`.

## Everything else

- **Portfolio (ask 7):** `PATCH /profiles/me/portfolio/{id}` takes any subset of fields and keeps the id and `created_at`.
- **Banks (asks 21, 45):** `GET /payments/banks` returns `[{"code", "name", "slug"}]` A–Z from Paystack, cached for 24 hours. Each bank code appears once: Paystack listed some banks twice under different names, e.g. "BANKIT MFB" and "BANKIT MICROFINANCE BANK LTD" with the same code, and the fuller name is kept. Banks that can't receive transfers (24 of them) are left out, so an artisan can't pick one we couldn't pay into. That's 258 banks today.
- **Data export (ask 13):** `GET /auth/me/export` returns one JSON file, served as a download. It covers account, profile, services, portfolio, gigs, bookings, payments, reviews, saved artisans, sent messages, notifications, support requests, verification status and the payout account. Secrets are left out; ID and bank numbers are masked.
- **Support (ask 14):** `POST /support/tickets {"subject", "message", "booking_id"?}` returns a real `ticket_number` (`SUP-XXXXXX`). `GET /support/tickets` lists the user's own. Admins use `/admin/support-tickets`.
- **Featured reviews (ask 40):**
  - `GET /reviews/featured?limit=6` (public) returns only reviews whose client ticked `share_publicly` when reviewing; the author can change this with `PATCH /reviews/{id}/sharing`.
  - Admin-featured reviews come first, then other 4–5 star ones.
  - Each item has `comment`, `rating`, `client_first_name`, `client_area` (state), `category` and an optional consented `photo_url`.
  - To collect consent, add a "share publicly" checkbox to the review form.
- **Gigs (ask 34):** `GET /gigs/?artisan_profile_id=…`.
- **Upload responses (ask 15):** all upload endpoints declare their response model in OpenAPI.
- **Favourites (asks 22, 43):** `{pro_id}` is the artisan's **user id**; their profile id is now accepted too. Each item from `GET /favorites/` now includes `first_name`, `last_name` and `artisan_profile_id`.
- **Errors (ask 33):**
  - Every unexpected error is a JSON `500 {"detail", "code": "server_error"}`, with CORS headers, never a dropped connection.
  - `"landmark_hint": null` is accepted on buy-gig.

## Delete account — facts for the discussion

What the backend does today, so we can agree the behaviour:

- `DELETE /auth/me` (an alias of `/auth/deactivate-me`) immediately sets the account inactive, records `deleted_at`, and revokes every session. Login then answers `423 account_deleted`. A deleted artisan drops out of search, and their profile page returns 404.
- **There is no anonymisation job yet.** The response says "data will be anonymised after the retention period", but nothing does that today, and no retention window is defined.
- There's no restore path. Open bookings and money held in escrow are left as they are. No password or code is asked for, and no email is sent.

Open questions for both teams: the retention window, whether a user can restore their account during it, what happens to open bookings and escrow, re-authentication before deleting, and a confirmation email with an undo link.
