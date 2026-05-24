// src/scenario/scenario.service.ts
import { PrismaClient } from "@prisma/client";
import fs from "fs-extra";
import path from "path";

export interface ScenarioEvent {
    tick: number;
    type:
        | 'CRASH_FAN'
        | 'LOAD_SPIKE_ALL'
        | 'THERMAL_DRIFT_SERVER'
        | 'CLEAR_THERMAL_DRIFT'
        | 'SET_FAN_SPEED_ALL'
        | 'SET_FAN_SPEED_SERVER'
        | 'RESTORE_FAN';
    targetId?: number;
    value?: number;
}

export interface Scenario {
    id: string;
    name: string;
    description: string;
    events: ScenarioEvent[];
}

export class ScenarioService {
    private activeScenario: Scenario | null = null;
    private currentTick: number = 0;
    private activeThermalDrifts: Map<number, number> = new Map();
    private currentLoadMultiplier: number = 1.0;
    private pendingRepairs: Map<number, { serverId: number; remainingTicks: number }> = new Map();

    constructor(private prisma: PrismaClient, private io: any) {}

    getCurrentScenario(): Scenario | null {
        return this.activeScenario;
    }

    getLoadMultiplier(): number {
        return this.currentLoadMultiplier;
    }

    getAllThermalDrifts(): Record<number, number> {
        return Object.fromEntries(this.activeThermalDrifts);
    }

    async loadScenario(scenarioId: string): Promise<void> {
        const filePath = path.join(__dirname, "../data_seed/scenarios.json");
        const scenarios: Scenario[] = await fs.readJson(filePath);
        
        const found = scenarios.find(s => s.id === scenarioId);
        if (!found) throw new Error(`Scénario [${scenarioId}] introuvable.`);

        this.activeScenario = found;
        this.currentTick = 0;
        this.activeThermalDrifts.clear();
        this.pendingRepairs.clear(); 
        this.currentLoadMultiplier = 1.0;
        
        console.log(`📖 Scénario armé : ${this.activeScenario.name}`);
        this.io.emit("scenario_started", { name: found.name, description: found.description });
    }

    async processTick(): Promise<void> {
        this.currentTick++;
        console.log(`⏱️ [SCÉNARIO] Tick : ${this.currentTick}`);

        // GESTION DU DÉLAI DE MAINTENANCE
        for (const [fanId, repair] of this.pendingRepairs.entries()) {
            repair.remainingTicks--;
            
            if (repair.remainingTicks > 0) {
                console.log(`🔧 [MAINTENANCE] Ventilateur ${fanId} en cours de remplacement...`);
                this.io.emit("maintenance_progress", { fanId, remainingTicks: repair.remainingTicks });
            } else {
                try {
                    await this.prisma.fan.update({
                        where: { fan_id: fanId },
                        data: { status: 'ON', control_mode: 'AUTO', speed_percent: 20 }
                    });
                    this.clearDriftForServer(repair.serverId);
                    console.log(`✅ [MAINTENANCE SUCCÈS] Remplacement du Ventilateur ${fanId} terminé.`);
                    this.io.emit("maintenance_complete", { fanId, serverId: repair.serverId });
                } catch (e) {
                    console.warn(`⚠️ [MAINTENANCE] Impossible de réparer le fan ${fanId} (ID obsolète pour cette topologie).`);
                } finally {
                    this.pendingRepairs.delete(fanId);
                }
            }
        }

        if (!this.activeScenario) return;

        const eventsToRun = this.activeScenario.events.filter(e => e.tick === this.currentTick);

        for (const event of eventsToRun) {
            console.log(`⚠️ ÉVÈNEMENT SCÉNARIO ENCLENCHÉ : [${event.type}]`);
            this.io.emit("scenario_event_triggered", event);

            // 🌟 US 1/3 SÉCURITÉ : Protection globale contre les IDs absents de la topologie courante
            try {
                switch (event.type) {
                    case 'CRASH_FAN':
                        if (event.targetId) {
                            await this.prisma.fan.update({
                                where: { fan_id: event.targetId },
                                data: { speed_percent: 0, control_mode: 'MANUAL', status: 'ON' }
                            });
                            console.log(`💥 [SCÉNARIO] Le ventilateur ${event.targetId} a été saboté.`);
                        }
                        break;

                    case 'LOAD_SPIKE_ALL':
                        if (event.value !== undefined) {
                            this.currentLoadMultiplier = event.value;
                            await this.prisma.sensor.updateMany({
                                where: { sensor_type: 'LOAD' },
                                data: { last_value: event.value * 50 }
                            });
                        }
                        break;

                    case 'THERMAL_DRIFT_SERVER':
                        if (event.targetId && event.value !== undefined) {
                            // On vérifie d'abord si le serveur existe dans notre topologie de test actuelle
                            const serverExists = await this.prisma.server.findUnique({ where: { server_id: event.targetId } });
                            if (serverExists) {
                                if (event.value === 0) {
                                    this.clearDriftForServer(event.targetId);
                                } else {
                                    this.activeThermalDrifts.set(event.targetId, event.value);
                                    console.log(`🔥 [SCÉNARIO] Dérive thermique (+${event.value}°C) appliquée au serveur ${event.targetId}`);
                                }
                            } else {
                                console.warn(`⏭️ [SCÉNARIO] Serveur cible ${event.targetId} absent de la topologie active. Événement ignoré.`);
                            }
                        }
                        break;

                    case 'CLEAR_THERMAL_DRIFT':
                        if (event.targetId) {
                            this.clearDriftForServer(event.targetId);
                        }
                        break;

                    case 'SET_FAN_SPEED_ALL':
                        if (event.value !== undefined) {
                            const speed = Math.max(0, Math.min(100, Math.round(event.value)));
                            await this.prisma.fan.updateMany({
                                where: { control_mode: 'AUTO' },
                                data: { speed_percent: speed, control_mode: 'AUTO', status: 'ON' }
                            });
                            console.log(`🌬️ [RÉGULATION] Tous les ventilateurs passent à ${speed}%.`);
                        }
                        break;

                    case 'SET_FAN_SPEED_SERVER':
                        if (event.targetId && event.value !== undefined) {
                            const speed = Math.max(0, Math.min(100, Math.round(event.value)));
                            await this.prisma.fan.updateMany({
                                where: { server_id: event.targetId, control_mode: 'AUTO' },
                                data: { speed_percent: speed, control_mode: 'AUTO', status: 'ON' }
                            });
                            console.log(`🌬️ [RÉGULATION] Ventilateurs du serveur ${event.targetId} à ${speed}%.`);
                        }
                        break;

                    case 'RESTORE_FAN':
                        if (event.targetId) {
                            const speed = Math.max(0, Math.min(100, Math.round(event.value ?? 35)));
                            await this.prisma.fan.update({
                                where: { fan_id: event.targetId },
                                data: { speed_percent: speed, control_mode: 'AUTO', status: 'ON' }
                            });
                            console.log(`✅ [RÉGULATION] Ventilateur ${event.targetId} restauré à ${speed}%.`);
                        }
                        break;
                }
            } catch (prismaError) {
                // L'erreur est interceptée ici : la simulation continue sa course !
                console.warn(`⏭️ [SCÉNARIO] Échec de l'événement ${event.type} pour la cible ${event.targetId} (Composant absent de la topologie). La simulation continue.`);
            }
        }
    }

    queueRepair(fanId: number, serverId: number, delayTicks: number): void {
        this.pendingRepairs.set(fanId, { serverId, remainingTicks: delayTicks });
    }

    isFanUnderRepair(fanId: number): boolean {
        return this.pendingRepairs.has(fanId);
    }

    getDriftForServer(serverId: number): number {
        return this.activeThermalDrifts.get(serverId) ?? 0.0;
    }

    clearDriftForServer(serverId: number): void {
        this.activeThermalDrifts.delete(serverId);
        console.log(`🧹 [SCÉNARIO] Pénalité environnementale/drift annulée pour le serveur ${serverId}`);
    }

    clearScenario(): void {
        this.activeScenario = null;
        this.currentTick = 0;
        this.activeThermalDrifts.clear();
        this.pendingRepairs.clear();
        this.currentLoadMultiplier = 1.0;
        console.log("♻️ Scénario nettoyé et dérives réinitialisées.");
    }
}
