/**
 * Contrôleur Stimulus pour gérer le partage des résultats de quiz sur les réseaux sociaux.
 * Permet de partager le score, les bonnes réponses et le lien vers le quiz.
 */
import { Controller } from "@hotwired/stimulus"

export default class extends Controller {
    static values = {
        score: Number,
        correctAnswers: Number,
        totalQuestions: Number,
        quizName: String,
        quizSlug: String,
        success: Boolean,
        perfectBonus: Boolean,
        comboMax: Number
    }

    // Cache pour stocker l'UUID du lien de partage créé
    shareUuid = null
    shareUrl = null

    /**
     * Génère le message de partage personnalisé
     */
    generateShareMessage() {
        const emoji = this.successValue ? "🎉" : "💪"
        const statusText = this.successValue ? "J'ai réussi" : "J'ai participé à"
        
        return `${emoji} ${statusText} le quiz "${this.quizNameValue}" !\n\n` +
               `📊 Score : ${this.scoreValue} points\n` +
               `✅ Bonnes réponses : ${this.correctAnswersValue}/${this.totalQuestionsValue}\n\n` +
               `🎮 Viens tester tes connaissances toi aussi !\n`
    }

    /**
     * Crée un lien de partage via l'API et retourne l'URL
     */
    async createShareLink(platform = null) {
        // Si déjà créé, retourner l'URL en cache
        if (this.shareUrl) {
            return this.shareUrl
        }

        try {
            const response = await fetch('/api/quiz/create-share-link', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    rule_set: this.quizSlugValue,
                    total_score: this.scoreValue,
                    total_correct_answers: this.correctAnswersValue,
                    total_questions: this.totalQuestionsValue,
                    success: this.successValue,
                    perfect_bonus: this.perfectBonusValue || false,
                    combo_max: this.comboMaxValue || 0,
                    platform: platform
                })
            })

            if (!response.ok) {
                throw new Error('Erreur lors de la création du lien')
            }

            const data = await response.json()
            this.shareUuid = data.uuid
            this.shareUrl = data.url

            return this.shareUrl
        } catch (error) {
            console.error('Erreur création lien de partage:', error)
            // Fallback : utiliser l'ancienne méthode
            return this.getQuizUrl()
        }
    }

    /**
     * Génère l'URL complète du quiz (fallback si l'API échoue)
     */
    getQuizUrl() {
        const baseUrl = window.location.origin
        return `${baseUrl}/play/${this.quizSlugValue}`
    }

    /**
     * Partage sur Facebook via l'API de partage
     * Note: Facebook ne permet plus de pré-remplir le texte pour des raisons de sécurité.
     * On copie donc le message dans le presse-papier et on informe l'utilisateur.
     */
    async shareOnFacebook(event) {
        event.preventDefault()
        const button = event.currentTarget
        const originalHTML = button.innerHTML
        
        try {
            // Afficher un état de chargement
            button.innerHTML = '<svg class="share-icon-svg" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="10" opacity="0.3"/></svg><span>Création...</span>'
            button.disabled = true
            
            // Créer le lien de partage personnalisé
            const url = await this.createShareLink('facebook')
            
            // Copier le message dans le presse-papier
            const message = this.generateShareMessage()
            await navigator.clipboard.writeText(message)
            
            // Feedback visuel
            button.innerHTML = '<svg class="share-icon-svg" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg><span>Message copié !</span>'
            
            // Ouvrir Facebook après un court délai
            setTimeout(() => {
                button.innerHTML = originalHTML
                button.disabled = false
                
                // URL de partage Facebook avec le lien personnalisé
                const facebookUrl = `https://www.facebook.com/sharer/sharer.php?u=${encodeURIComponent(url)}`
                
                // Ouvrir dans une popup
                this.openShareWindow(facebookUrl, 'Partager sur Facebook')
            }, 1000)
            
        } catch (err) {
            console.error('Erreur lors du partage Facebook', err)
            
            button.innerHTML = originalHTML
            button.disabled = false
            
            // Fallback : utiliser l'ancienne méthode
            const fallbackUrl = this.getQuizUrl()
            const facebookUrl = `https://www.facebook.com/sharer/sharer.php?u=${encodeURIComponent(fallbackUrl)}`
            this.openShareWindow(facebookUrl, 'Partager sur Facebook')
        }
    }

    /**
     * Partage sur Twitter/X
     */
    async shareOnTwitter(event) {
        event.preventDefault()
        const button = event.currentTarget
        const originalHTML = button.innerHTML
        
        try {
            // Afficher un état de chargement
            button.innerHTML = '<svg class="share-icon-svg" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="10" opacity="0.3"/></svg><span>Création...</span>'
            button.disabled = true
            
            // Créer le lien de partage personnalisé
            const url = await this.createShareLink('twitter')
            const text = this.generateShareMessage()
            
            // Restaurer le bouton
            button.innerHTML = originalHTML
            button.disabled = false
            
            const twitterUrl = `https://twitter.com/intent/tweet?text=${encodeURIComponent(text)}&url=${encodeURIComponent(url)}`
            
            this.openShareWindow(twitterUrl, 'Partager sur Twitter')
            
        } catch (err) {
            console.error('Erreur lors du partage Twitter', err)
            
            button.innerHTML = originalHTML
            button.disabled = false
            
            // Fallback
            const fallbackUrl = this.getQuizUrl()
            const text = this.generateShareMessage()
            const twitterUrl = `https://twitter.com/intent/tweet?text=${encodeURIComponent(text)}&url=${encodeURIComponent(fallbackUrl)}`
            this.openShareWindow(twitterUrl, 'Partager sur Twitter')
        }
    }

    /**
     * Partage via l'API Web Share native (mobile friendly)
     */
    async shareNative(event) {
        event.preventDefault()
        
        if (navigator.share) {
            try {
                await navigator.share({
                    title: `Quiz ${this.quizNameValue}`,
                    text: this.generateShareMessage(),
                    url: this.getQuizUrl()
                })
            } catch (err) {
                // L'utilisateur a annulé le partage ou erreur
                console.log('Partage annulé', err)
            }
        } else {
            // Fallback : copier dans le presse-papier
            this.copyToClipboard(event)
        }
    }

    /**
     * Copie le lien et le message dans le presse-papier
     */
    async copyToClipboard(event) {
        event.preventDefault()
        
        const shareText = this.generateShareMessage() + '\n' + this.getQuizUrl()
        
        try {
            await navigator.clipboard.writeText(shareText)
            
            // Feedback visuel
            const button = event.currentTarget
            const originalText = button.innerHTML
            button.innerHTML = '✅ Copié !'
            button.classList.add('btn-success-flash')
            
            setTimeout(() => {
                button.innerHTML = originalText
                button.classList.remove('btn-success-flash')
            }, 2000)
        } catch (err) {
            console.error('Erreur lors de la copie', err)
            alert('Impossible de copier le lien')
        }
    }

    /**
     * Ouvre une fenêtre popup pour le partage
     */
    openShareWindow(url, title) {
        const width = 600
        const height = 400
        const left = (window.innerWidth - width) / 2
        const top = (window.innerHeight - height) / 2
        
        window.open(
            url,
            title,
            `width=${width},height=${height},left=${left},top=${top},toolbar=0,menubar=0,location=0,status=0`
        )
    }
}
