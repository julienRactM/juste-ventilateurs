import "dotenv/config";
import { Kafka } from "kafkajs";
import { prisma } from '../prisma/prisma';

const KAFKA_BROKERS = (process.env.KAFKA_BROKERS || "kafka-broker:9092").split(",");
const TOPIC = "telemetry";
const GROUP_ID = "dc-consumer-stateless";

const kafka = new Kafka({ clientId: "dc-consumer", brokers: KAFKA_BROKERS });
const admin = kafka.admin();
const consumer = kafka.consumer({ groupId: GROUP_ID });

async function main() {
  // Ensure the topic exists before subscribing — Kafka won't auto-create it for consumers
  await admin.connect();
  await admin.createTopics({
    topics: [{ topic: TOPIC, numPartitions: 3, replicationFactor: 1 }],
  });
  await admin.disconnect();

  await consumer.connect();
  await consumer.subscribe({ topic: TOPIC, fromBeginning: false });
  console.log('📥 Consumer connecté et en écoute du jumeau numérique...');

  await consumer.run({
    eachMessage: async ({ message }) => {
      if (!message.value) return;

      try {
        const payload = JSON.parse(message.value.toString());
        const virtualTime = new Date(payload.timestamp);

        if (!payload.sensors || !Array.isArray(payload.sensors)) return;

        for (const sensor of payload.sensors) {
          const sensorId = Number(sensor.id);
          const sensorValue = parseFloat(sensor.value);

          if (isNaN(sensorId) || isNaN(sensorValue)) continue;

          try {
            console.log(`➔ [DB TRY] Tentative d'insertion pour le capteur ${sensorId} (Valeur: ${sensorValue})...`);

            await prisma.sensorData.create({
              data: { sensor_id: sensorId, value: sensorValue, time: virtualTime }
            });

            console.log(`  ✔ [DB SUCCESS] Capteur ${sensorId} inséré avec succès !`);

            await prisma.sensor.update({
              where: { sensor_id: sensorId },
              data: { last_value: sensorValue }
            });

          } catch (dbError: any) {
            if (dbError.code === 'P2003') {
              console.warn(`⚠️ [MÉMOIRE] Capteur obsolète ignoré (ID: ${sensorId}). La topologie a été reconstruite.`);
              continue;
            }
            console.error(`❌ Erreur d'écriture pour le capteur ${sensorId} :`, dbError.message);
          }
        }

      } catch (err) {
        console.error('❌ Erreur générale d\'analyse du message Kafka :', err);
      }
    }
  });
}

main().catch(err => {
  console.error("❌ Erreur fatale du consumer Kafka :", err);
  process.exit(1);
});
