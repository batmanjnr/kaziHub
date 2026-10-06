# KaziHub Backend - Developer & Agent Guide (GEMINI.md)

Welcome to the backend repository of **KaziHub**, a full-featured service marketplace connecting clients with skilled local artisans. This document serves as the primary instructional context, architectural blueprint, and development playbook for both human developers and AI assistants.

---

## 1. Project Overview

KaziHub Backend is built using the **FastAPI** framework and leverages **Beanie ODM** (Object Document Mapper) over a **MongoDB** database (via the async **Motor** driver). It includes features for staged user authentication with OTP email verification, dynamic artisan profiles, booking & escrow state-management, real-time WebSocket chat, portfolio showcases, and a secure transaction ledger. Media uploads are handled securely via **Cloudinary**.

### Core Stack & Technologies
*   **Language:** Python 3.10+
*   **Web Framework:** [FastAPI](https://fastapi.tiangolo.com/) (fully asynchronous)
*   **Database ODM:** [Beanie ODM](https://beanie-odm.dev/) (Pydantic-based ODM for MongoDB)
*   **Async MongoDB Driver:** Motor / PyMongo
*   **Media Storage:** [Cloudinary](https://cloudinary.com/) (HTTPS-based image/file management)
*   **Security & Auth:** Bcrypt for hashing, PyJWT & python-jose for signed JSON Web Tokens
*   **Real-time Communication:** FastAPI WebSockets with a custom state-based ConnectionManager
*   **Email Services:** `smtplib` utilizing background tasks
*   **Environment & Configuration:** Pydantic Settings (`pydantic-settings`) loading from a `.env` file

---

## 2. Directory Structure

Below is an overview of the backend's directory structure to help you quickly locate components:

```text
kaziHub/
├── app/
│   ├── main.py                     # App factory: Beanie init, middleware, /api/v1 router
│   ├── worker.py                   # APScheduler background jobs (python -m app.worker)
│   ├── api/
│   │   ├── deps.py                 # Auth dependencies (user, artisan, admin+2FA, own profile)
│   │   └── v1/
│   │       ├── router.py           # V1 API entrypoint router
│   │       └── endpoints/          # account (data export), admin, auth, bookings, chat,
│   │                               # favorites, gigs, notifications, payments, portfolio,
│   │                               # profiles, reviews, services, support, verification, wallet
│   ├── core/                       # constants + validators (closed value sets, field rules),
│   │                               # errors (APIError codes, JSON 500s), time (UTC now),
│   │                               # config, security (JWT/bcrypt), encryption (AES-GCM PII),
│   │                               # nin_hash, two_factor, rate_limit, security_headers,
│   │                               # idempotency, ws_ticket, kyc_upload_token,
│   │                               # upload_validation, cloudinary, websocket_manager
│   ├── models/                     # Beanie documents + request/response schemas
│   ├── schemas/                    # Standalone request schemas (auth)
│   └── services/
│       ├── booking_transitions.py  # Optimistic-locked booking state machine (all status changes)
│       ├── payouts.py              # Claim-then-pay escrow release via Paystack Transfer
│       ├── paystack.py             # Paystack API wrapper
│       ├── notifications.py        # notify(): the one way notifications are created
│       ├── email.py, sms.py, moderation.py
│       └── jobs/                   # auto_release, cleanup, email_summary, notify, webhook_retry
├── docs/FRONTEND_API_NOTES.md      # Flows, WebSocket protocol, field rules for the frontend
├── scripts/migrate_frontend_asks.py # One-off live-data clean-up (dry run by default)
├── tests/                          # pytest + mongomock-motor suite (never touches a real DB)
├── requirements.txt
└── test_mail.py                    # Direct script for testing SMTP connection
```

---

## 3. Building, Configuration, and Running

### Prerequisites
*   Python 3.10 or higher
*   Running MongoDB instance (Local or Atlas)
*   SMTP Server credentials (e.g. Mailtrap sandbox)
*   **Cloudinary Account** (for API Key/Secret)

### 1. Configuration (Environment Variables)
The system expects configuration variables to be declared in a `.env` file at the root directory. Copy or create a `.env` file with the following keys:

```ini
MONGODB_URL=mongodb+srv://<username>:<password>@cluster.mongodb.net/
DATABASE_NAME=kazihub_db
SECRET_KEY=generate_a_secure_long_random_string_here
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=10080  # 7 days

SMTP_HOST=sandbox.smtp.mailtrap.io
SMTP_PORT=2525
SMTP_USER=your_smtp_username
SMTP_PASSWORD=your_smtp_password
EMAILS_FROM_EMAIL=noreply@kazihub.com

CLOUDINARY_CLOUD_NAME=your_cloud_name
CLOUDINARY_API_KEY=your_api_key
CLOUDINARY_API_SECRET=your_api_secret
```

### 2. Setting Up Virtual Environment and Dependencies
Activate your virtual environment and install the required libraries:

```bash
# Create virtual environment if not present
python -m venv venv

# Activate on macOS/Linux:
source venv/bin/activate

# Install dependencies:
pip install -r requirements.txt
```

### 3. Testing SMTP Setup
A dedicated mail delivery test script is available at the root to check your SMTP server credentials before launching:

```bash
python test_mail.py
```

### 4. Running the Server
The application is run via **Uvicorn**:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
Once started, the interactive OpenAPI documentation is available at:
*   Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
*   ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

## 4. Architectural Details & Data Models

KaziHub uses **Beanie ODM**, where models inherit from Beanie's `Document` and are represented in the codebase as models containing relational links (`Link[User]`). All document models are registered inside the FastAPI `lifespan` in `app/main.py`.

### Document Models Overview
1.  **User (`users`)**: Represents all registered accounts. Contains standard fields like name, email (uniquely indexed), phone, NIN, hashed password, state, role (`"client"` or `"artisan"`), preferences (`theme`, `preferred_language`), and verification settings.
2.  **PendingUser (`pending_users`)**: Staged storage for user registration. New users sign up, their hashed credentials and a 5-digit verification code (OTP) are kept here temporarily. The user is only written to the main `users` table after OTP verification.
3.  **Profile (`profiles`)**: Links to an artisan's `User` account (`Link[User]`). Stores skills, hourly rate, years of experience, category, city/state, availability, and a verification flag.
4.  **Gig (`gigs`)**: Stores pre-defined packages or services offered by an artisan. It links to the creator's `User` document.
5.  **Booking (`bookings`)**: Tracks agreements, quote exchanges, and jobs. Supports 3 service types: `fixed_service`, `custom_quote`, and `gig_purchase`.
6.  **Conversation (`conversations`)**: Represents a message room between a client and an artisan. Keeps track of the active booking, last message, and updated timestamp.
7.  **Message (`messages`)**: Contains chat text, voice URLs, files, or state updates (`message_type` of `"text"`, `"image"`, `"voice"`, `"quote_offer"`, or `"booking_update"`).
8.  **Transaction (`transactions`)**: Escrow journal tracking. Logs payments with statuses: `escrow_deposit`, `escrow_release`, or `refund`.
9.  **Verification (`verifications`)**: Keeps records of submitted ID details (e.g. NIN, ID Card, Selfie) and verification review states (`pending`, `approved`, `rejected`).

---

## 5. Main Workflows

### A. Authentication & Sign-Up Flow
1.  **Registration (`POST /api/v1/auth/register`)**: Stashes input data into `PendingUser` with a generated 5-digit OTP. Dispatches an email asynchronously in a FastAPI `BackgroundTasks`.
2.  **Email Verification (`POST /api/v1/auth/verify-email`)**: Compares OTP. On success, transfers user information to `User` collection. If the user's role is `"artisan"`, an empty `Profile` skeleton is automatically created.
3.  **Login (`POST /api/auth/login` / standard OAuth2 Form)**: Verifies password against hashed password in db using bcrypt (with a safe 72-byte max length guard for inputs). Returns a JWT token containing the `sub` claim.

### B. Chat & WebSockets
1.  **REST Fetching**: Call `POST /api/v1/chat/conversations` to get or spin up an active session ID between a client and artisan.
2.  **WebSocket Chat (`WS /api/v1/chat/ws/{conversation_id}?token={token}`)**:
    *   Authenticates incoming connections with JWT query token.
    *   Saves active sockets in a single `ConnectionManager` map.
    *   On message, inserts a `Message` document, updates the `last_message` and `updated_at` properties in the corresponding `Conversation`, and broadcasts JSON to all active sockets connected to that room.

### C. Booking State Machine
Bookings progress through strict statuses:
```text
[QUOTE_REQUESTED (Custom)] ──> [QUOTE_SENT]
        │                           │
        └──────> [PENDING] <────────┘ (Fixed/Gig purchase start here)
                     │
                     └──> [ACCEPTED]
                               │
                               └──> [ESCROW_FUNDED] (Payment locked)
                                         │
                                         └──> [IN_PROGRESS]
                                                   │
                                                   └──> [COMPLETED_BY_ARTISAN]
                                                             │
                                                             └──> [PAID_OUT] (Funds Released)
```
*At any stage before ESCROW_FUNDED, a booking can be CANCELLED. If things go wrong during progress, a client or artisan can mark it as DISPUTED.*

---

## 6. Coding & Contribution Conventions

If you are modifying or extending this codebase, please observe the following conventions:

1.  **Asynchronous by Default**: All DB access and endpoints MUST use `async` / `await` syntax since Motor and Beanie are fully async.
2.  **Explicit Type Annotations**: Leverage Pydantic’s powerful validation. Always declare response models (`response_model=...`) on route operations to filter database hashes or private fields.
3.  **Strict Security Practices**: Never log or serialize raw passwords. Password checks must bypass python-jose's internal signature checks and use the functions found in `app/core/security.py`.
4.  **Route DNS Fallbacks**: The codebase patches DNS resolution internally in `app/main.py`. Keep this configuration intact to maintain connection reliability over varying ISP routers.
5.  **Media Uploads**: All file/image uploads must go through the `app/core/cloudinary.py` helper to ensure secure HTTPS URL storage.
6.  **Dependency Injection**: Use `Depends(get_current_user)` to authenticate routes. For artisan-only workflows, inject `Depends(get_current_artisan)` to verify role permissions cleanly.


