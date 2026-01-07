import requests
import time
import subprocess
import os
import signal
import sys
import json

def verify():
    # Start the server in a subprocess
    print("Starting server...")
    process = subprocess.Popen([sys.executable, "run_server.py"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    
    try:
        # Wait for server to start
        print("Waiting for server to start...")
        time.sleep(5) 
        
        # Check health
        try:
            health = requests.get("http://localhost:5000/health")
            if health.status_code == 200:
                print("Server is healthy!")
            else:
                print(f"Server health check failed: {health.status_code}")
                return False
        except requests.exceptions.ConnectionError:
            print("Could not connect to server.")
            return False

        # Test tool call simulating n8n payload (with extra fields)
        payload = {
            "tool": "get_service_guideline",
            "input": {
                "category": "core",
                "sessionId": "12345", # Extra field that caused error
                "action": "test",
                "chatInput": "hello",
                "toolCallId": "abc"
            }
        }
        
        print(f"Sending test payload: {json.dumps(payload, indent=2)}")
        
        response = requests.post("http://localhost:5000/call", json=payload)
        
        if response.status_code == 200:
            print("SUCCESS: Tool call succeeded despite extra arguments!")
            print(f"Response: {response.json()}")
            return True
        else:
            print(f"FAILURE: Tool call failed with status {response.status_code}")
            print(f"Response: {response.text}")
            return False
            
    finally:
        print("Stopping server...")
        try:
            os.kill(process.pid, signal.SIGTERM)
        except:
            process.terminate() # fallback
            
        stdout, stderr = process.communicate(timeout=5)
        if stdout: print(f"Server STDOUT: {stdout}")
        if stderr: print(f"Server STDERR: {stderr}")

if __name__ == "__main__":
    success = verify()
    if not success:
        sys.exit(1)
