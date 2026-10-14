# Employee Attendance & Analytics API

A FastAPI and MongoDB backend service implementing business rules for attendance tracking (punch-in, punch-out, corrections with audit trails) and high-performance analytics powered by MongoDB aggregation pipelines.

## Project Structure
- `app/main.py`: Complete FastAPI application with all endpoints, helpers, models, and startup index creations.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Detailed code review of defects found in the starter code with concrete failing examples and fixes.
- `DECISIONS.md`: Architecture and design rationale covering indexes, concurrency, ranking ties, headcount, and 100x scalability.
- `sample_data/` & `sample_seed.py`: Sample documents and seeding utility.

## Setup & Running

### Prerequisites
- Python 3.11+
- MongoDB 6.0+ (Local instance, Docker, or MongoDB Atlas)

### Setup
```bash
python -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Environment Configuration
Set `MONGO_URI` and `MONGO_DB` as environment variables or inside a `.env` file:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### Start the Service
```bash
uvicorn app.main:app --port 8000
```
Indexes are automatically created at startup.
