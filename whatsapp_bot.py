#!/usr/bin/env python3
import logging, os
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
from common import analyze_code, ocr_image_url, CFG

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    incoming_msg = request.values.get("Body", "").strip()
    num_media = int(request.values.get("NumMedia", "0"))
    resp = MessagingResponse()
    reply = resp.message()

    if num_media > 0:
        media_url = request.values.get("MediaUrl0")
        extracted = ocr_image_url(media_url)
        if not extracted.strip():
            reply.body("No pude leer texto de la imagen. ¿Puedes enviar el error en texto?")
        else:
            analysis = analyze_code(extracted)
            reply.body(analysis)
    elif incoming_msg:
        analysis = analyze_code(incoming_msg)
        reply.body(analysis)
    else:
        reply.body("Envíame el error o una captura para analizarlo.")
    return Response(str(resp), mimetype="application/xml")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
