import requests

url = "https://betplay.com.co/reverse-proxy/accounts/me/balance"

headers = {
    "authorization": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6IjQyQTRKSE8zODQ4NTRKMjZTNjk0IiwiaWRQZXIiOjg4MjM1MjYsInR5cGUiOiJBdXRoZW50aWNhdGlvbiIsImNvdW50cnlDb2RlIjoiQ08iLCJpYXQiOjE3NzQ1MTU4MDQsImV4cCI6MTc3NDUxNjQwNH0.Eazd29NiBnbkh7h_6uxljo3Ekk6Z_F-_LYbj_esJhlP4C7X-BAvgidWNVQP",
    "x-custom-header": "1095915302",
    "x-custom-version": "4.0.54"
}

response = requests.get(url, headers=headers)

print(response.json())