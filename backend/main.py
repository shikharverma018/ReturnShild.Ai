from fastapi import FastAPI, Depends, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
import pandas as pd
import io

import models
import schemas
import ml_model
from database import engine, get_db

# Create DB tables
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="ReturnShield AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"message": "Welcome to ReturnShield AI API"}

@app.post("/upload", response_model=schemas.UploadResponse)
async def upload_transactions(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="Only CSV files are allowed")
    
    try:
        contents = await file.read()
        try:
            # Use BytesIO to let pandas handle the decoding efficiently
            df = pd.read_csv(io.BytesIO(contents), encoding='utf-8')
        except UnicodeDecodeError:
            # Fallback for ISO-8859-1 or Windows-1252 files with special characters (like £/€)
            df = pd.read_csv(io.BytesIO(contents), encoding='latin1')
        
        # Define possible aliases for required columns
        column_map = {
            'InvoiceNo': ['InvoiceNo', 'Invoice No', 'OrderNo', 'Order No', 'Invoice'],
            'StockCode': ['StockCode', 'Stock Code', 'ProductCode', 'Product Code', 'SKU'],
            'Description': ['Description', 'Item', 'Product'],
            'Quantity': ['Quantity', 'Qty', 'Amount'],
            'InvoiceDate': ['InvoiceDate', 'Invoice Date', 'OrderDate', 'Order Date', 'Date'],
            'UnitPrice': ['UnitPrice', 'Unit Price', 'Price', 'Rate'],
            'user_id': ['user_id', 'user id', 'CustomerID', 'Customer ID', 'UserID', 'User ID', 'UID', 'id'],
            'Country': ['Country', 'Region', 'Location'],
            'is_fraud': ['is_fraud', 'is fraud', 'fraud', 'fraudulent', 'label']
        }
        
        # User dataset might have 'is_fraud' or not (since it's an anomaly detection task)
        # We will strip whitespace from columns just in case
        df.columns = df.columns.str.strip()
        
        # Dynamic Column Detection
        found_map = {}
        for internal_name, aliases in column_map.items():
            # Try exact match first, then case-insensitive, then alias check
            match = next((col for col in df.columns if col.lower() == internal_name.lower()), None)
            if not match:
                for alias in aliases:
                    match = next((col for col in df.columns if col.lower() == alias.lower()), None)
                    if match:
                        break
            
            if match:
                found_map[match] = internal_name
            else:
                # If critical column is missing, throw error
                if internal_name in ['InvoiceNo', 'Quantity', 'UnitPrice', 'user_id', 'InvoiceDate']:
                    raise HTTPException(status_code=400, detail=f"Could not find a column representing '{internal_name}'. Please ensure your CSV has a column like '{aliases[0]}'.")

        # Rename columns to internal standard
        df = df.rename(columns=found_map)
        
        # Analyze data using ML model
        analysis_df = ml_model.analyze_returns(df)
        
        # Clear existing data for demo simplicity (optional, can just update)
        db.query(models.RiskExplanation).delete()
        db.query(models.Transaction).delete()
        db.query(models.User).delete()
        db.commit()
        
        # Save Users and Explanations
        for _, row in analysis_df.iterrows():
            # Clean user_id (remove trailing .0 if it's a numeric ID read as a float)
            raw_uid = str(row['user_id'])
            clean_uid = raw_uid.removesuffix('.0')
            
            user = models.User(
                id=clean_uid,
                total_orders=int(row['total_orders']),
                total_returns=int(row['total_returns']),
                avg_return_time_days=float(row['avg_return_time_days']),
                risk_score=float(row['risk_score']),
                is_fraud=bool(row['is_fraud'])
            )
            db.add(user)
            
            for reason in row['reasons']:
                explanation = models.RiskExplanation(user_id=clean_uid, reason=reason)
                db.add(explanation)
                
        # Save Transactions (Optional, depending on scale. Doing it for completion)
        # Limiting to 500 records saved to the DB to prevent massive DB insertion times for huge hackathon files
        for _, row in df.head(500).iterrows():
            item_val = float(row['UnitPrice']) if pd.notnull(row['UnitPrice']) else 0.0
            qty = int(row['Quantity']) if pd.notnull(row['Quantity']) else 0
            
            # Clean user_id
            raw_uid = str(row['user_id'])
            clean_uid = raw_uid.removesuffix('.0')
            
            # If Quantity is negative, it indicates a return
            is_ret = qty < 0
            
            txn = models.Transaction(
                user_id=clean_uid,
                order_date=pd.to_datetime(row['InvoiceDate']).to_pydatetime() if pd.notnull(row['InvoiceDate']) else None,
                return_date=None, # Not explicitly in new dataset, implied by negative qty
                item_value=abs(item_val * qty),
                is_returned=is_ret,
                receipt_reused=False # Not available in new schema
            )
            db.add(txn)
            
        db.commit()
        return {"message": "Transactions processed and analyzed successfully", "rows_processed": len(df)}
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/dashboard/stats", response_model=schemas.DashboardStats)
def get_dashboard_stats(db: Session = Depends(get_db)):
    total_users = db.query(models.User).count()
    high_risk_users = db.query(models.User).filter(models.User.is_fraud == True).count()
    
    # Calculate suspicious returns
    suspicious_returns = db.query(models.Transaction).join(models.User).filter(
        models.User.is_fraud == True,
        models.Transaction.is_returned == True
    ).count()
    
    avg_score_raw = db.query(func.avg(models.User.risk_score)).scalar()
    avg_score = float(avg_score_raw) if avg_score_raw is not None else 0.0
    
    return {
        "total_users_analyzed": int(total_users),
        "high_risk_users": int(high_risk_users),
        "suspicious_returns": int(suspicious_returns),
        "average_risk_score": float(round(float(avg_score), 1))
    }

@app.get("/users/suspicious", response_model=list[schemas.UserOut])
def get_suspicious_users(db: Session = Depends(get_db), limit: int = 10):
    users = db.query(models.User).order_by(models.User.risk_score.desc()).limit(limit).all()
    
    result = []
    for u in users:
        reasons = [r.reason for r in u.risk_explanations]
        result.append(schemas.UserOut(
            id=u.id,
            total_orders=u.total_orders,
            total_returns=u.total_returns,
            avg_return_time_days=u.avg_return_time_days,
            risk_score=u.risk_score,
            is_fraud=u.is_fraud,
            reasons=reasons
        ))
    return result

@app.get("/users/{user_id}", response_model=schemas.UserOut)
def get_user_details(user_id: str, db: Session = Depends(get_db)):
    u = db.query(models.User).filter(models.User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
        
    reasons = [r.reason for r in u.risk_explanations]
    return schemas.UserOut(
        id=u.id,
        total_orders=u.total_orders,
        total_returns=u.total_returns,
        avg_return_time_days=u.avg_return_time_days,
        risk_score=u.risk_score,
        is_fraud=u.is_fraud,
        reasons=reasons
    )
