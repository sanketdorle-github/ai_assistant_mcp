/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#f5f7ff",
          100: "#ebeeff",
          200: "#d3daff",
          300: "#aab6ff",
          400: "#7c8bff",
          500: "#5563f5",
          600: "#3f46d6",
          700: "#3336ab",
          800: "#2b2e87",
          900: "#272a6b",
        },
      },
      keyframes: {
        shimmer: {
          "0%": { backgroundPosition: "200% 0" },
          "100%": { backgroundPosition: "-200% 0" },
        },
      },
      animation: {
        shimmer: "shimmer 2.2s linear infinite",
      },
    },
  },
  plugins: [],
};
