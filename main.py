import os
exec("try:HERE=os.path.dirname(os.path.abspath(__file__));_=sys.path.remove(HERE) if HERE in sys.path else 0;sys.path.insert(0,HERE)\nexcept:HERE=os.getcwd()") # HERE for interactive run
DEBUG=os.getenv('FLASK_ENV')=='development'

from flask import Flask, request, jsonify
app = Flask(__name__)

@app.route('/')
def hello():
	return "Hello World!"

@app.route('/info')
def info():
	return request.headers
	resp = {
		'connecting_ip': request.headers['X-Real-IP'],
		'proxy_ip': request.headers['X-Forwarded-For'],
		'host': request.headers['Host'],
		'user-agent': request.headers['User-Agent']
	}
	return jsonify(resp)

if __name__ in "__main__":
	# default port 5000; not use 80, otherwise browser switches to https
	# for production, this would not run
    app.run(host="0.0.0.0", port=5000, debug=True)

# from fastapi import FastAPI
# app = FastAPI()

# @app.get("/")
# def root():
#     return {"status": "ok", "service": "dockerapp_template"}

# @app.get("/health")
# def health():
#     return {"status": "healthy"}
