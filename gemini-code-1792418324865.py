import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
from pyxirr import xirr
from datetime import datetime
import pdfplumber
import re
import tempfile
import os

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="MFD Portfolio Analyzer", layout="wide")
st.title("📊 MFD Portfolio vs Benchmark Analyzer")
st.write("Upload your client's mutual fund statement to compare their returns against the Nifty 500 index.")

# --- HELPER FUNCTIONS ---
@st.cache_data(ttl=3600) 
def fetch_benchmark_data(ticker_symbol, start_date, end_date):
    try:
        df_idx = yf.download(ticker_symbol, start=start_date, end=end_date, progress=False)
        if isinstance(df_idx.columns, pd.MultiIndex):
            return df_idx['Close'].iloc[:, 0]
        return df_idx['Close']
    except Exception as e:
        st.error(f"Failed to fetch benchmark data: {e}")
        return None

def calculate_true_benchmark(benchmark_series, df_trans, valuation_date):
    if benchmark_series is None or benchmark_series.empty:
        return None, 0.0
        
    df_cashflows = df_trans[df_trans['Type'] == 'CASHFLOW'].sort_values('Date').copy()
    cumulative_units = 0.0
    bm_dates = []
    bm_amounts = []
    
    for idx, row in df_cashflows.iterrows():
        d = row['Date']
        amt = row['Amount']
        
        try:
            price = benchmark_series.asof(d)
            if pd.isna(price): price = benchmark_series.dropna().iloc[0]
        except:
            price = benchmark_series.dropna().iloc[0]
            
        if amt < 0:
            cumulative_units += abs(amt) / price
        else:
            cumulative_units -= amt / price
            
        bm_dates.append(d)
        bm_amounts.append(amt)
        
    try:
        latest_price = benchmark_series.asof(valuation_date)
        if pd.isna(latest_price): latest_price = benchmark_series.dropna().iloc[-1]
    except:
        latest_price = benchmark_series.dropna().iloc[-1]
        
    simulated_val = cumulative_units * latest_price
    bm_dates.append(valuation_date)
    bm_amounts.append(simulated_val) 
    
    try:
        return xirr(bm_dates, bm_amounts), simulated_val
    except:
        return None, simulated_val

# --- FILE UPLOAD & PROCESSING ---
uploaded_file = st.file_uploader("Upload Client Statement (.xlsx or .pdf)", type=["xlsx", "pdf"])
df = None
ready_to_calculate = False
val_date_str = datetime.today().strftime('%Y-%m-%d')

if uploaded_file is not None:
    file_extension = uploaded_file.name.split('.')[-1].lower()
    
    # === PDF PROCESSING ===
    if file_extension == 'pdf':
        st.info("If your PDF is password protected (usually your PAN), enter it below. If it is already unlocked, leave this blank.")
        pdf_password = st.text_input("🔒 PDF Password (Optional):", type="password")
        
        if st.button("Analyze Portfolio"):
            with st.spinner("Unlocking, reading formatting, and consolidating market values..."):
                pwd_to_use = pdf_password if pdf_password else ""
                
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                    tmp_file.write(uploaded_file.getvalue())
                    tmp_file_path = tmp_file.name

                try:
                    cashflows = []
                    total_market_value = 0.0
                    total_cost_value = 0.0
                    statement_start_date = None
                    
                    with pdfplumber.open(tmp_file_path, password=pwd_to_use) as pdf:
                        for page in pdf.pages:
                            text = page.extract_text()
                            if text:
                                if not statement_start_date:
                                    period_match = re.search(r'(\d{2}-[A-Za-z]{3}-\d{4})\s+To\s+(\d{2}-[A-Za-z]{3}-\d{4})', text, re.IGNORECASE)
                                    if period_match:
                                        statement_start_date = pd.to_datetime(period_match.group(1))

                                val_matches = re.findall(r'Market Value on (\d{2}-[A-Za-z]{3}-\d{4}).*?((?:\d+,)*\d+\.\d{2})', text, re.IGNORECASE)
                                for match in val_matches:
                                    val_date_str = match[0]
                                    total_market_value += float(match[1].replace(',', ''))
                                    
                                cost_matches = re.findall(r'Total Cost Value:.*?((?:\d+,)*\d+\.\d{2})', text, re.IGNORECASE)
                                for match_str in cost_matches:
                                    total_cost_value += float(match_str.replace(',', ''))
                                
                                for line in text.split('\n'):
                                    if any(kw in line.upper() for kw in ['PURCHASE', 'SIP', 'REDEEM', 'SWITCH', 'INSTALMENT']):
                                        match = re.search(r'(\d{2}-[A-Za-z]{3}-\d{4})[\s|]+(.*?)[\s|]+((?:\d+,)*\d+\.\d{2}(?!\d))', line)
                                        if match:
                                            date_str = match.group(1)
                                            desc = match.group(2).upper()
                                            amt = float(match.group(3).replace(',', ''))
                                            
                                            if any(kw in desc for kw in ['REDEEM', 'PAYOUT', 'WITHDRAW', 'SWITCH OUT']):
                                                amt = abs(amt) 
                                            else:
                                                amt = -abs(amt) 

                                            cashflows.append({
                                                'Scheme': 'PDF Portfolio',
                                                'Date': pd.to_datetime(date_str),
                                                'Amount': amt,
                                                'Type': 'CASHFLOW'
                                            })
                                            
                    if cashflows:
                        parsed_invested = sum(abs(cf['Amount']) for cf in cashflows if cf['Amount'] < 0)
                        missing_historical_cost = total_cost_value - parsed_invested
                        
                        if missing_historical_cost > 10: 
                            inject_date = statement_start_date if statement_start_date else min([cf['Date'] for cf in cashflows])
                            cashflows.append({
                                'Scheme': 'Opening Balance (Historical Cost)',
                                'Date': inject_date,
                                'Amount': -missing_historical_cost,
                                'Type': 'CASHFLOW'
                            })

                        df = pd.DataFrame(cashflows)
                        st.success("Mapped transactions & historical opening balances successfully!")
                        
                        if total_market_value > 0:
                            val_row = pd.DataFrame([{
                                'Scheme': 'PDF Portfolio',
                                'Date': pd.to_datetime(val_date_str),
                                'Amount': total_market_value,
                                'Type': 'VALUATION'
                            }])
                            df = pd.concat([df, val_row], ignore_index=True)
                            ready_to_calculate = True
                        else:
                            st.warning("Could not auto-detect the final market value.")
                    else:
                        st.error("Could not find any standard CAMS/KFintech transactions.")
                        
                except Exception as e:
                    st.error(f"Failed to process PDF. Error details: {str(e)}")
                finally:
                    if os.path.exists(tmp_file_path):
                        os.remove(tmp_file_path)

    # === EXCEL PROCESSING ===
    elif file_extension == 'xlsx':
        if st.button("Analyze Portfolio"):
            try:
                df = pd.read_excel(uploaded_file)
                df = df.dropna(how='all', axis=1).dropna(how='all', axis=0)
                df.columns = [str(c).strip() for c in df.columns]
