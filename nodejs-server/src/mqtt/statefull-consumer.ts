import "dotenv/config";
import { Kafka } from "kafkajs";
import pg from "pg";
import { PrismaPg } from "@prisma/adapter-pg";
import { PrismaClient } from '@prisma/client';

const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });
const adapter = new PrismaPg(pool);
const prisma = new PrismaClient({ adapter });

const KAFKA_BROKERS = (process.env.KAFKA_BROKERS || "kafka-broker:9092").split(",");
const TOPIC = "telemetry";
const GROUP_ID = "dc-consumer-statefull";

const kafka = new Kafka({ clientId: "dc-consumer-statefull", brokers: KAFKA_BROKERS });
const consumer = kafka.consumer({ groupId: GROUP_ID });

async function main() {
  await consumer.connect();
  await consumer.subscribe({ topic: TOPIC, fromBeginning: false });
  console.log("📥 Consumer FULL connecté (Mode: Update + Insert)");

  await consumer.run({
    eachMessage: async ({ message }) => {
      if (!message.value) return;

      try {
        const payload = JSON.parse(message.value.toString());
        const timestamp = new Date(payload.timestamp);

        for (const s of payload.sensors) {
          const val = parseFloat(s.value);

          await prisma.sensor.update({
            where: { sensor_id: s.id },
            data: { last_value: val }
          });

          await prisma.sensorData.create({
            data: { sensor_id: s.id, value: val, time: timestamp }
          });
        }
        console.log(`[HIST] ${payload.hostname} enregistré.`);

      } catch (err) {
        console.error("❌ Erreur stockage complet:", err);
      }
    }
  });
}

main().catch(err => {
  console.error("❌ Erreur fatale du consumer FULL Kafka :", err);
  process.exit(1);
});
