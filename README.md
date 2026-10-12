# Employee Attendance & Analytics API


A REST API built with FastAPI and MongoDB for employee management, attendance tracking, regularization, and analytics.


## Requirements
- Python 3.10 or newer
- MongoDB 6.0 or newer


## Setup
1. Create a virtual environment: `python -m venv venv`
2. Activate it: `.\venv\Scripts\Activate.ps1`
3. Install dependencies: `python -m pip install -r requirements.txt`
4. Set MongoDB URI: ` $env:MONGO_URI = 'mongodb://localhost:27017' `
5. Set database name: ` $env:MONGO_DB = 'attendance_db' `
6. Seed sample data if needed: `python sample_seed.py`
7. Start the API: `uvicorn app.main:app --host 127.0.0.1 --port 8000`

API documentation: http://127.0.0.1:8000/docs

## Main Endpoints
- `GET /health`
- `POST /employees`
- `GET /employees`
- `POST /attendance/punch-in`
- `POST /attendance/punch-out`
- `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

## Project Files
- `app/main.py` — API implementation
- `requirements.txt` — Dependencies
- `REVIEW.md` — Starter-code review
- `DECISIONS.md` — Design decisions
- `DATA_MODEL.md` — Data model
- `openapi.yaml` — API contract

## Validation
Python syntax compilation passed and selected API endpoints were tested locally. This does not guarantee all hidden evaluation cases pass.

## Security
Do not commit `.env` files, credentials, virtual environments, caches, or other secrets.


## Main Endpoints
- `GET /health`
- `POST /employees`
- `GET /employees`
- `POST /attendance/punch-in`
- `POST /attendance/punch-out`
- `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

## Project Files
- `app/main.py` — API implementation
- `requirements.txt` — Dependencies
- `REVIEW.md` — Starter-code review
- `DECISIONS.md` — Design decisions
- `DATA_MODEL.md` — Data model
- `openapi.yaml` — API contract

## Validation
Python syntax compilation passed and selected API endpoints were tested locally. This does not guarantee all hidden evaluation cases pass.

## Security
Do not commit `.env` files, credentials, virtual environments, caches, or other secrets.
