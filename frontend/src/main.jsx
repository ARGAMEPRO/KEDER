import React from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/noto-sans/400.css'
import '@fontsource/noto-sans/600.css'
import '@fontsource/noto-sans/800.css'
import '@fontsource/jetbrains-mono/400.css'
import './index.css'
import App from './App.jsx'

createRoot(document.getElementById('root')).render(<App />)
