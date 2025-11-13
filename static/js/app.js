/**
 * Point d'entrée de l'application JavaScript.
 * Configure et initialise Stimulus pour gérer les interactions frontend.
 */
import { Application } from "@hotwired/stimulus"
import ShareController from "./controllers/share_controller.js"

// Initialise l'application Stimulus
const application = Application.start()

// Configure le mode debug pour le développement
application.debug = false
window.Stimulus = application

// Enregistre les contrôleurs
application.register("share", ShareController)

// Export pour accès global si nécessaire
export { application }
