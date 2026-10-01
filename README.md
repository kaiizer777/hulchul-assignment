# Hulchul Assignment

A full-stack application built with Next.js (App Router, TypeScript, Tailwind CSS) and FastAPI (Python).

---

## Project Structure

```text
hulchul-assignment/
├── frontend/   # Next.js (TypeScript, Tailwind CSS, App Router)
├── backend/    # FastAPI (Python, Uvicorn, Playwright, Groq)
├── .gitignore  # Root gitignore
└── README.md   # Setup instructions
```

---

## Getting Started

### Prerequisites

- Node.js (v18+ recommended)
- Python (v3.10+ recommended)
- `npm` / `pnpm` / `yarn`

---

### Backend Setup

1. Navigate to the `backend` directory:
   ```bash
   cd backend
   ```

2. Activate the virtual environment:
   - **Windows (PowerShell)**:
     ```powershell
     .\.venv\Scripts\Activate.ps1
     ```
   - **macOS / Linux**:
     ```bash
     source .venv/bin/activate
     ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Install Playwright browsers (if using browser automation):
   ```bash
   playwright install
   ```

5. Run the FastAPI development server:
   ```bash
   uvicorn main:app --reload --port 8000
   ```

   The API will be available at `http://localhost:8000` (Swagger docs at `http://localhost:8000/docs`).

---

### Frontend Setup

1. Navigate to the `frontend` directory:
   ```bash
   cd frontend
   ```

2. Install dependencies (if not already installed):
   ```bash
   npm install
   ```

3. Run the development server:
   ```bash
   npm run dev
   ```

   The frontend will be running at `http://localhost:3000`.
