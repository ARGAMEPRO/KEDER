import React from 'react'
import { createRoot } from 'react-dom/client'
// Only the subsets the UI can actually render (Cyrillic + Latin): the default
// imports pull in Devanagari/Greek/Vietnamese and triple the font payload.
import '@fontsource/noto-sans/cyrillic-400.css'
import '@fontsource/noto-sans/latin-400.css'
import '@fontsource/noto-sans/cyrillic-600.css'
import '@fontsource/noto-sans/latin-600.css'
import '@fontsource/noto-sans/cyrillic-800.css'
import '@fontsource/noto-sans/latin-800.css'
import '@fontsource/jetbrains-mono/cyrillic-400.css'
import '@fontsource/jetbrains-mono/latin-400.css'
import './index.css'
import App from './App.jsx'

createRoot(document.getElementById('root')).render(<App />)
