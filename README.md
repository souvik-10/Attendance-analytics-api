# Employee Attendance & Analytics API



A REST API built with **Python 3.11+, FastAPI, and MongoDB Atlas** for employee management, attendance tracking, regularization, and analytics.



## Features



- Employee creation and listing

- Attendance punch-in and punch-out

- Attendance listing and regularization

- Monthly employee analytics

- Department attendance summaries

- Late-arrival leaderboard

- Department attendance trends

- Administrative query-plan explanations



## Requirements



- Python 3.11 or newer

- MongoDB Atlas account and database access

- MongoDB Atlas connection string



## Setup



Create and activate a virtual environment in PowerShell:



```powershell

python -m venv .venv

.\.venv\Scripts\Activate.ps1

```



Install dependencies:



```powershell

pip install -r requirements.txt

```



Set the MongoDB Atlas connection details in your PowerShell session:



```powershell

$env:MONGO_URI = "<your MongoDB Atlas connection string>"

$env:MONGO_DB = "attendance_db"

```



Replace the placeholder with your actual connection string. Configure the appropriate database user and network access in MongoDB Atlas. Never commit credentials or secrets to GitHub.



## Run the API



```powershell

uvicorn app.main:app --port 8000

```



API documentation:



- Swagger UI: http://127.0.0.1:8000/docs

- OpenAPI JSON: http://127.0.0.1:8000/openapi.json



The application requires MongoDB Atlas configuration and does not use a local MongoDB fallback.



## Repository



The repository contains the application source, dependency list, code review, design decisions, and this README.
