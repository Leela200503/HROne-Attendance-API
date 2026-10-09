# Employee Attendance & Analytics API

FastAPI and MongoDB application to manage employee attendance, track punch-in and punch-out events, manual corrections, and generate analytics.

## Prerequisites
- Python 3.11+
- MongoDB 6.0+

## Setup
1. Create a virtual environment and install dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. Configure environment variables in a `.env` file (optional, defaults provided):
   ```env
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```
3. Run the application:
   ```bash
   uvicorn app.main:app --port 8000
   ```

## Design Decisions and Review
- Check out `DECISIONS.md` for design decisions and index explanations.
- Check out `REVIEW.md` for a list of defects found in the starter code and how they were fixed.
