import json
from time import perf_counter

import boto3
from botocore.config import Config


def main():
    session = boto3.Session(
        profile_name="commander-rag",
        region_name="eu-north-1",
    )

    client = session.client(
        "bedrock-runtime",
        config=Config(
            connect_timeout=10,
            read_timeout=60,
            retries={"mode": "standard", "total_max_attempts": 1},
        ),
    )

    cases = [
        (
            "CON Rest in Peace",
            "Un oponente controla una criatura y Rest in Peace.",
        ),
        (
            "SIN Rest in Peace",
            "Un oponente controla una criatura. "
            "No hay Rest in Peace en el campo.",
        ),
    ]

    for case_name, opponent_board in cases:
        prompt = f"""
        FUENTES
        Los siguientes textos son resúmenes de reglas y cartas.

        [S1] Definición de morir — Comprehensive Rules 700.4:
        Una criatura muere cuando pasa del campo de batalla a un cementerio.
        Salir del campo de batalla hacia otra zona no cumple esta definición.

        [S2] Efectos de reemplazo — Comprehensive Rules 614.6:
        Si un evento es reemplazado, el evento original no sucede.
        Sucede el evento modificado en su lugar.
        Las habilidades se disparan según los eventos que realmente ocurren.

        [S3] Blood Artist:
        Siempre que Blood Artist u otra criatura muera, su habilidad se dispara.
        La condición no exige que la criatura sea controlada por ningún
        jugador concreto.
        Cada criatura que cumple la condición produce un disparo.
        Si Blood Artist muere simultáneamente con otras criaturas,
        también se cuentan las muertes de esas otras criaturas.

        [S4] Rest in Peace:
        Si una carta o ficha fuera a ir a un cementerio desde cualquier lugar,
        se exilia en su lugar.
        Este efecto solo se aplica si Rest in Peace está en el campo de batalla
        y conserva su habilidad.

        ESTADO DEL CAMPO DE BATALLA
        Estas son todas las criaturas presentes:
        - C1: Blood Artist, bajo mi control.
        - C2: otra criatura, bajo mi control.
        - C3: otra criatura, bajo mi control.
        - C4: otra criatura, bajo el control de un oponente.

        Otros permanentes relevantes:
        {opponent_board}

        La criatura del oponente mencionada en esa frase es C4;
        no es una criatura adicional.

        Ninguna criatura es comandante.
        No hay otros efectos, habilidades ni respuestas relevantes.

        ACCIÓN
        Se resuelve Wrath of God, cuya instrucción es destruir todas las
        criaturas. Hay que determinar el destino de C1, C2, C3 y C4
        teniendo en cuenta los efectos activos.

        PREGUNTA
        ¿Cuántas veces se dispara la habilidad de mi Blood Artist?

        RESPUESTA
        Responde en español usando exactamente estos cuatro campos:

        Destino final: indica a qué zona van C1, C2, C3 y C4.
        Criaturas que mueren: indica cuántas cumplen la definición de [S1].
        Disparos de Blood Artist: escribe un único número entero.
        Justificación: explica brevemente el resultado y cita las fuentes
        pertinentes mediante sus identificadores.

        No añadas un resumen inicial ni repitas el número de disparos
        en la justificación.
        """

        print(f"\nCASO: {case_name}")
        start = perf_counter()

        response = client.converse(
            modelId="eu.amazon.nova-2-lite-v1:0",
            system=[
                {
                    "text": (
                        "Resuelve consultas de reglas de Magic usando únicamente "
                        "las fuentes y la situación proporcionadas. "
                        "Las fuentes describen efectos posibles; la situación "
                        "determina cuáles están activos. "
                        "Usa las definiciones técnicas de las reglas. "
                        "Cuenta únicamente los eventos que realmente ocurren "
                        "después de aplicar los efectos pertinentes. "
                        "Devuelve una respuesta breve y coherente: los números "
                        "deben concordar con el destino de las criaturas y con "
                        "la condición de la habilidad. "
                        "Si falta información imprescindible, pide una aclaración."
                    )
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [{"text": prompt}],
                }
            ],
            inferenceConfig={
                "maxTokens": 1000,
                "temperature": 0,
            },
        )

        for block in response["output"]["message"]["content"]:
            if "text" in block:
                print(block["text"])

        print(f"\nLatencia: {perf_counter() - start:.2f} segundos")
        print("Tokens:", json.dumps(response.get("usage", {})))
        print("Finalización:", response.get("stopReason"))


if __name__ == "__main__":
    main()


